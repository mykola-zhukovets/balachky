"""Оркестрація пакета «Прослухати» (§3.2, §8): звʼязує TtsSidecar + координатор +
плеєр в одну точку для UI.

Політика одночасності — PARENT-only (§3.2):
  • playback (кнопка/хоткей) — **latest-wins**: новий запит скасовує поточний
    (cancel + join старого воркера) і стартує свій;
  • експорт у файл — **reject-busy**: якщо синтез уже йде, повертаємо "busy"
    (тост), не перебиваючи.

Витіснення STT — через HeavyModelCoordinator.acquire_tts (lifecycle-стан, не обхід).
STT зайнятий (запис) → TTS-запит чесно відхиляється, без утримання locks.

Плеєр (QMediaPlayer) чіпається ЛИШЕ в GUI-потоці, тож важкий синтез іде у воркер-
потоці, а готовий combined-WAV віддається назад через колбек, що маршалить у GUI
(app інжектить сигнал; тести — записувач). Клас навмисно НЕ QObject — тестується
без QApplication; уся Qt-взаємодія через інжектовані колбеки/плеєр."""
from __future__ import annotations

import logging
import os
import shutil
import threading

from whisper_core.tts import (FAKE_ENGINE_MARKER, MSG_ACCEPTED, MSG_CHUNK_READY,
                              save, voices)
from whisper_core.tts.plaintext_temp import PlaintextAudioDir
from whisper_core.tts.sidecar import TtsSidecar, TtsSidecarError

_log = logging.getLogger("balachky.tts")


class TtsController:
    def __init__(self, *, cfg, coordinator, resolve_voice=None,
                 sidecar_factory=None, temp_factory=None, combine=None,
                 on_playable=None, on_export_done=None, on_timings=None,
                 on_chunk_playable=None, stop_playback=None, available_langs=None,
                 lexicon_provider=None, position_provider=None, toast=None,
                 on_synth_dropped=None):
        self._cfg = cfg
        self._coord = coordinator
        self._resolve = resolve_voice or voices.resolve
        self._available_langs_fn = available_langs or self._default_available_langs
        self._make_sidecar = sidecar_factory or TtsSidecar
        self._temp_factory = temp_factory or PlaintextAudioDir
        self._combine = combine or save.combine_wavs
        self._on_playable = on_playable or (lambda path: None)
        self._on_export_done = on_export_done or (lambda path: None)
        self._on_timings = on_timings or (lambda words, starts: None)  # караоке (Хв.2)
        # СТРІМІНГ (§3.2 TTFS): перший chunk_ready → плеєру НЕГАЙНО, не після combine.
        # (token, wav_path, timings, is_first) — маршал у GUI робить app.
        self._on_chunk_playable = on_chunk_playable or (lambda tok, p, t, first: None)
        # суд 5.3: playback-генерація завершилась БЕЗ жодного відданого плеєру чанка
        # (скасування/помилка/відхилення/порожньо/fake) → зняти resume-arm тієї генерації,
        # щоб наступний незалежний потік не успадкував чужу позицію. Несе generation-токен.
        self._on_synth_dropped = on_synth_dropped or (lambda tok: None)
        self._stop_playback = stop_playback or (lambda: None)  # звільнити файл плеєра
        # словник вимови (Хвиля 4): провайдер IPC-знімка активного профілю+голосу
        self._lexicon_provider = lexicon_provider or (lambda voice_id: [])
        # зворотне диктування (Хвиля 5, §9.2): провайдер поточної позиції відтворення
        # (індекс речення/чанка) — None, коли нічого не грає.
        self._position_provider = position_provider or (lambda: None)
        self._reverse_state = None         # {sentence_index} коли reverse поставив паузу
        self._toast = toast or (lambda key: None)
        self._sidecar = None
        self._active_req = None            # id активного synthesize (для cancel)
        self._busy = False                 # синтез у польоті (reject-busy)
        self._worker = None
        # request-local ownership token (§8.9): кожен запит володіє СВОЄЮ temp-текою;
        # старий _run після join-timeout НЕ чіпає стан/теку нового (гонка власності).
        self._lock = threading.Lock()
        self._token = 0
        self._active_token = None
        self._active_temp = None           # PlaintextAudioDir активного запиту
        self._retry_dirs = []              # шляхи, які rmtree не зміг (Windows-лок) — повтор

    # --- публічний стан ---
    def is_busy(self) -> bool:
        return self._busy

    def sidecar(self):
        return self._sidecar

    def active_req(self):
        return self._active_req

    def last_generation(self):
        """Суд 5.3: generation-токен ОСТАННЬОГО стартованого synth-запиту. Викликається
        одразу після play_text(...)=='playing', щоб armed-resume прив'язати саме до нього
        (панель споживає arm лише чанком цієї генерації)."""
        return self._token

    # --- вибір голосу за мовою тексту ---
    def _voice_id_for(self, lang: str) -> str:
        if str(lang).lower() == "en":
            return getattr(self._cfg, "tts_voice_en", "kokoro_en")
        return getattr(self._cfg, "tts_voice_uk", "styletts2_ua")

    def _resolve_active(self, lang: str):
        return self._resolve(self._voice_id_for(lang), lang)

    def _default_available_langs(self) -> set:
        """Мови, для яких є ЗАВАНТАЖЕНИЙ голос (для розвʼязання unknown/mixed §7.2)."""
        langs = set()
        for lg in ("uk", "en"):
            vid = self._voice_id_for(lg)
            try:
                if voices.voice_available(vid):
                    langs.add(lg)
            except Exception:              # noqa: BLE001
                pass
        return langs

    def _available_langs(self) -> set:
        try:
            return set(self._available_langs_fn())
        except Exception:                  # noqa: BLE001
            return set()

    def _guard_voice(self, text: str):
        """Спільна перевірка: enabled/порожньо/мова/наявність голосу.
        Повертає (rv, lang, outcome): outcome!=None → рано вийти з кодом."""
        if not getattr(self._cfg, "tts_enabled", False):
            self._toast("tts_disabled_note")
            return None, "", "disabled"
        text = (text or "").strip()
        if not text:
            return None, "", "empty"
        lang = voices.detect_language(text)
        if lang in ("unknown", "mixed"):
            # §7.2: не синтезувати наосліп. Якщо доступний РІВНО один мовний голос —
            # беремо його (неоднозначності нема); якщо кілька — просимо вибір користувача;
            # якщо жодного — фолбек на UI-мову (далі однаково впаде в no_voice).
            avail = self._available_langs()
            if len(avail) == 1:
                lang = next(iter(avail))
            elif len(avail) > 1:
                self._toast("tts_lang_pick")
                return None, "", "lang_pick"
            else:
                lang = getattr(self._cfg, "ui_language", "uk")
        rv = self._resolve_active(lang)
        if rv == voices.LANGUAGE_MISMATCH:
            self._toast("tts_lang_mismatch")
            return None, lang, "lang_mismatch"
        if rv is None or not rv.available():
            self._toast("tts_no_voice_hint")
            return None, lang, "no_voice"
        return rv, lang, None

    # --- prewarming (§2) ---
    def prewarm(self, lang: str = "uk") -> None:
        """Прогрів рушія (§2): піднімати процес воркера та вантажити голос наперед."""
        if not getattr(self._cfg, "tts_enabled", False):
            return
        rv = self._resolve_active(lang)
        if rv is None or isinstance(rv, str) or not rv.available():
            return
        def _do_prewarm():
            try:
                lease = self._coord.acquire_tts(active=False)
                if lease is None:
                    return
                with self._lock:
                    if self._sidecar is None:
                        self._sidecar = self._make_sidecar()
                    sc = self._sidecar
                sc.start()
                sc.load_voice(rv.id, engine=rv.engine_kind, manifest_path=rv.manifest_path)
            except Exception:                    # noqa: BLE001
                pass
        threading.Thread(target=_do_prewarm, daemon=True).start()

    # --- playback: latest-wins ---
    def play_text(self, text: str, *, source_start_cp: int = 0) -> str:
        rv, _lang, outcome = self._guard_voice(text)
        if outcome is not None:
            return outcome
        self._preempt_previous()           # latest-wins: скасувати/приєднати/hard-kill старий
        if not self._start(text.strip(), source_start_cp, rv, export=False):
            return "rejected"
        return "playing"

    # --- export: reject-busy ---
    def export_text(self, text: str, out_path: str) -> str:
        if self._busy:
            self._toast("tts_busy")        # reject-busy: не перебиваємо активний синтез
            return "busy"
        rv, _lang, outcome = self._guard_voice(text)
        if outcome is not None:
            return outcome
        if not self._start(text.strip(), 0, rv, export=True, out_path=out_path):
            return "rejected"
        return "exporting"

    def stop(self) -> None:
        """Зупинити відтворення й скасувати синтез (кнопка «Стоп»)."""
        self._preempt_previous()

    # --- зв'язка зі зворотним диктуванням (Хвиля 5, §9.2) --------------------
    def mark_reverse_pause(self) -> bool:
        """Зворотне диктування активується під час озвучення: ЗАПАМʼЯТАТИ позицію
        (індекс речення) і поставити TTS на паузу ДО мікрофона. True — була активна
        озвучка (є що відновлювати). Ordinary-запис цей метод НЕ викликає, тож resume
        для нього недосяжний (мікрофон перемагає, §9.1)."""
        idx = None
        try:
            idx = self._position_provider()
        except Exception:                  # noqa: BLE001
            idx = None
        if idx is None or idx < 0:
            return False                   # нічого не грає — нема що відновлювати
        self._reverse_state = {"sentence_index": int(idx)}
        self.stop()                        # пауза playback + скасування синтезу
        return True

    def has_reverse_pending(self) -> bool:
        return self._reverse_state is not None

    def consume_reverse_index(self) -> "int | None":
        """Забрати збережений індекс речення (і скинути стан). None — reverse-паузи не
        було (ordinary-запис → TTS автоматично НЕ відновлюється)."""
        state, self._reverse_state = self._reverse_state, None
        return state["sentence_index"] if state else None

    def _cleanup_dir(self, temp) -> None:
        """Прибрати КОНКРЕТНУ temp-теку (§8.9): плеєр уже звільнив файл. Невдача
        (Windows-лок на handle) → шлях у retry-list, повтор на наступному cleanup."""
        if temp is None:
            return
        self._drain_retry()                # спершу добити раніше залоковані
        try:
            ok = temp.cleanup()            # True/None=ок; False=не вдалося
        except Exception:                  # noqa: BLE001
            ok = False
        if ok is False:
            path = getattr(temp, "path", "")
            if path:
                self._retry_dirs.append(path)

    def _drain_retry(self) -> None:
        if not self._retry_dirs:
            return
        import shutil as _sh
        still = []
        for p in self._retry_dirs:
            _sh.rmtree(p, ignore_errors=True)
            if os.path.isdir(p):
                still.append(p)            # ще залоковано — спробуємо пізніше
        self._retry_dirs = still

    def _preempt_previous(self) -> None:
        """§8.9 ownership: скасувати активний запит, звільнити плеєр, приєднати воркер;
        якщо застряг понад дедлайн — hard-kill sidecar (successor підніме свіжий), і
        лише ТОДІ прибрати ЙОГО temp. Успішний cooperative cancel → чистимо теку."""
        with self._lock:
            sc, req = self._sidecar, self._active_req
            w = self._worker
            temp = self._active_temp
            self._active_token = None      # старий _run більше не «активний» (гонка)
        cooperative = True
        if sc is not None and req:
            try:
                cooperative = sc.cancel(req)   # False → hard-kill усередині
            except Exception:              # noqa: BLE001
                cooperative = True
        joined = True
        if w is not None and w.is_alive():
            w.join(timeout=3.0)
            joined = not w.is_alive()
        if not (cooperative and joined):
            # застряг у нативному forward → hard-kill і викидаємо sidecar (свіжий далі)
            if sc is not None:
                try:
                    sc.hard_kill()
                except Exception:          # noqa: BLE001
                    pass
            with self._lock:
                self._sidecar = None
        try:
            self._stop_playback()          # звільнити файл плеєра ПЕРЕД видаленням теки
        except Exception:                  # noqa: BLE001
            pass
        with self._lock:
            self._active_req = None
            self._busy = False
            if self._active_temp is temp:
                self._active_temp = None
        self._cleanup_dir(temp)            # чистимо лише СВОЮ (цього запиту) теку

    def _start(self, text, cp, rv, *, export, out_path=None) -> bool:
        lease = self._coord.acquire_tts()
        if lease is None:                  # STT зайнятий (запис) — чесна відмова
            self._toast("tts_muted_recording")
            return False
        with self._lock:
            if self._sidecar is None:
                self._sidecar = self._make_sidecar()
            self._token += 1
            token = self._token
            temp = self._temp_factory()
            self._active_token = token
            self._active_temp = temp
            self._busy = True
            self._worker = threading.Thread(
                target=self._run, args=(token, temp, text, cp, rv, export, out_path),
                daemon=True)
            worker = self._worker
        worker.start()
        return True

    def _is_active(self, token) -> bool:
        with self._lock:
            return self._active_token == token

    def _run(self, token, temp, text, cp, rv, export, out_path) -> None:
        wavs = []
        chunk_timings = []                 # per-речення word_timings (локальні media-ms)
        state = {"fake": False, "first": True}

        def notify_dropped():
            # суд 5.3/5.4: генерація завершилась, не віддавши жодного playable-чанка
            # плеєру → зняти resume-arm цієї генерації. Критерій — ФАКТ доставки, а не
            # порожнеча wavs: on_event додає wav у wavs БЕЗУМОВНО, але _on_chunk_playable
            # викликає лише для АКТИВНОГО token; при preempt (token знеактивнено) wav
            # осідає в wavs, чанк НЕ доставлено, state["first"] лишається True. Лише playback.
            if not export and state["first"]:
                try:
                    self._on_synth_dropped(token)
                except Exception:          # noqa: BLE001
                    pass

        def safe_toast(key):
            # суд 5.5: тост НЕ має зривати drop/cleanup — notify_dropped викликаємо ПЕРШИМ,
            # тост обгортаємо, щоб виняток трею не пропустив _finish (витік temp/arm).
            try:
                self._toast(key)
            except Exception:              # noqa: BLE001
                pass

        def on_event(m):
            t = m.get("type")
            if t == MSG_ACCEPTED:
                if self._is_active(token):
                    with self._lock:
                        self._active_req = m.get("id")
            elif t == MSG_CHUNK_READY and m.get("wav_path"):
                if FAKE_ENGINE_MARKER in (m.get("normalized_text") or ""):
                    state["fake"] = True   # відхилимо fake-marker (CRITICAL 2)
                wavs.append(m["wav_path"])
                chunk_timings.append(m.get("timings") or [])
                # СТРІМІНГ (§3.2 TTFS): перший готовий чанк — плеєру НЕГАЙНО (не після
                # combine); наступні — у prefetch-чергу панелі. Лише playback, не export.
                if not export and self._is_active(token) and not state["fake"]:
                    self._on_chunk_playable(token, m["wav_path"],
                                            m.get("timings") or [], state["first"])
                    state["first"] = False

        try:
            lex_snapshot = self._lexicon_provider(rv.id)   # §6.2 словник вимови профілю
        except Exception:                  # noqa: BLE001
            lex_snapshot = []
        try:
            self._sidecar.load_voice(rv.id, engine=rv.engine_kind,
                                     manifest_path=rv.manifest_path)
            self._sidecar.synthesize_stream(
                text=text, voice_id=rv.id, wav_dir=temp.path,
                source_start_cp=cp, engine=rv.engine_kind,
                want_timings=not export, lexicon_snapshot=lex_snapshot,
                on_event=on_event)
        except TtsSidecarError:
            notify_dropped()               # суд 5.5: drop ГАРАНТОВАНО перед тостом
            safe_toast("tts_engine_error")
            self._finish(token, temp)
            return
        except Exception:                  # noqa: BLE001
            _log.exception("Помилка синтезу озвучення")
            notify_dropped()               # суд 5.5: drop ГАРАНТОВАНО перед тостом
            safe_toast("tts_engine_error")
            self._finish(token, temp)
            return
        finally:
            if self._is_active(token):
                with self._lock:
                    self._active_req = None
                    self._busy = False
        # відхилення fake-marker (CRITICAL 2): відсутня модель ≠ «успіх тиші»
        if state["fake"]:
            notify_dropped()               # суд 5.5: drop ГАРАНТОВАНО перед тостом
            safe_toast("tts_engine_error")
            self._finish(token, temp)
            return
        if not wavs:
            notify_dropped()               # скасовано/порожньо ДО першого чанка → disarm
            self._finish(token, temp)      # порожній результат → прибрати свою теку
            return
        if export and out_path:
            self._do_export(token, wavs, out_path, temp)
            return
        # playback: чанки вже віддані плеєру стрімінгом (on_chunk_playable); combined
        # НЕ будуємо, TTFS збережено. Temp живе, доки грає, до наступного preempt.
        # Суд 5.4 (тихий вихід): wavs непорожній, ТА жодного чанка не доставлено плеєру
        # (preempt зняв активність token ПІСЛЯ появи WAV у wavs) → arm лишився б висіти.
        # notify_dropped спрацює лише коли state["first"] ще True (нічого не доставлено).
        notify_dropped()

    def _do_export(self, token, wavs, out_path, temp) -> None:
        combined = os.path.join(temp.path, "combined.wav")
        try:
            self._combine(wavs, combined)
            if not save.enough_free_space(os.path.dirname(out_path) or ".",
                                          os.path.getsize(combined)):
                self._toast("tts_save_nospace")
                self._finish(token, temp)      # нестача місця → прибрати, без частк. файлу
                return
            shutil.copyfile(combined, out_path)
            self._on_export_done(out_path)
        except OSError:
            _log.exception("Не вдалося зберегти озвучення")
            self._toast("tts_engine_error")
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)    # прибрати частковий експорт (§8.7)
            except OSError:
                pass
        self._finish(token, temp)          # експорт не грає combined → одразу прибрати

    def _finish(self, token, temp) -> None:
        """Завершити запит: прибрати ЙОГО теку (якщо не грає). Скидаємо busy лише
        якщо це ще активний запит (гонка власності §8.9)."""
        if token is not None and self._is_active(token):
            with self._lock:
                self._busy = False
                self._active_req = None
                if self._active_temp is temp:
                    self._active_temp = None
        self._cleanup_dir(temp)

    def shutdown(self) -> None:
        """Завершити sidecar і прибрати temp (вихід застосунку/мік-гейт). Викликається
        з app aboutToQuit — інакше plaintext-аудіо лишилось би після виходу (§8.9)."""
        self._preempt_previous()
        with self._lock:
            sc, self._sidecar = self._sidecar, None
            temp, self._active_temp = self._active_temp, None
        if sc is not None:
            try:
                sc.shutdown()
            except Exception:              # noqa: BLE001
                pass
        self._cleanup_dir(temp)
        self._drain_retry()
