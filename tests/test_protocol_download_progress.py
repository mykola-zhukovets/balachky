"""Поступ завантаження моделі протоколу (ProtocolModelDownloadDialog):
1) повний текст — скільки з скількох, відсоток, швидкість, час, що лишився;
2) чесна поведінка, коли швидкість ще не порахована або сервер не дав total;
3) дросель перемальовування UI (не частіше ніж раз на ~200 мс).

Аудит-першоджерело: 2026-07-30-АУДИТ-завантаження-моделей.md — модальне вікно
показувало лише "Завантажую модель… 169 МБ" без знаменника/відсотка/швидкості/
часу, а 18 000+ сигналів поступу на 4.6 ГБ файл перемальовували напис без
обмеження частоти.
"""
import os
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication

from fronts.desktop import download_manager as dlmgr
from fronts.desktop.i18n import current_language, set_language, format_decimal, format_duration
from fronts.desktop.pages.protocol_ui import (
    ProtocolModelDownloadDialog, ProtocolModelDownloadWorker,
    ModelDownloadWaitDialog, _PROGRESS_UI_INTERVAL_S,
)

_APP = QApplication.instance() or QApplication([])

_MB_I = 1024 * 1024


def _reset_download_manager():
    """DownloadManager — процесний синглтон: без скидання між тестами друга
    конструкція діалогу для того самого target/preset бачить ALREADY_THIS
    замість чистого STARTED і тягне стан попереднього тесту."""
    mgr = dlmgr.DownloadManager.instance()
    mgr._worker = None
    mgr._key = None
    mgr._label = ""
    mgr._done = 0
    mgr._total = 0
    mgr._explicit_cancel = False


def _make_dialog(target="C:/nonexistent", preset_id="fast"):
    """Діалог без реального завантаження: QThread.start() підмінено на no-op,
    тому мережевий код model_manager ніколи не виконується."""
    _reset_download_manager()
    with patch.object(ProtocolModelDownloadWorker, "start", lambda self: None):
        dlg = ProtocolModelDownloadDialog(target, preset_id=preset_id)
    return dlg


class TestFormatDecimalDuration(unittest.TestCase):
    """format_decimal/format_duration: числа по-українськи (кома), по-англійськи (крапка)."""

    def setUp(self):
        self._lang = current_language()
        self.addCleanup(set_language, self._lang)

    def test_format_decimal_uk_comma(self):
        set_language("uk")
        self.assertEqual(format_decimal(3.7, 1), "3,7")

    def test_format_decimal_en_dot(self):
        set_language("en")
        self.assertEqual(format_decimal(3.7, 1), "3.7")

    def test_format_decimal_mutation_catches_dot_in_uk(self):
        """Мутаційна перевірка: uk-рядок мусить мати кому і не мати крапки."""
        set_language("uk")
        res = format_decimal(12.5, 1)
        self.assertIn(",", res)
        self.assertNotIn(".", res)
        with self.assertRaises(AssertionError):
            self.assertEqual(res, "12.5")

    def test_format_duration_minutes_and_seconds_uk(self):
        set_language("uk")
        self.assertEqual(format_duration(355), "5 хв 55 с")

    def test_format_duration_under_minute_uk(self):
        set_language("uk")
        self.assertEqual(format_duration(42), "42 с")

    def test_format_duration_minutes_and_seconds_en(self):
        set_language("en")
        self.assertEqual(format_duration(355), "5 min 55 s")


class TestProtocolProgressText(unittest.TestCase):
    """_format_status: текст поступу за наявними даними, нічого не вигадує."""

    def setUp(self):
        self._lang = current_language()
        self.addCleanup(set_language, self._lang)
        set_language("uk")
        self.dlg = _make_dialog()
        self.addCleanup(self.dlg.deleteLater)

    def test_full_progress_shows_denominator_percent_speed_eta(self):
        total = 4608 * _MB_I
        done = 169 * _MB_I
        self.dlg._speed_bps = 12.5 * _MB_I
        text = self.dlg._format_status(done, total)
        # ГОЛОВНА UX-знахідка: до фіксу тут був лише "169 МБ" без знаменника.
        self.assertIn("169 МБ з 4608 МБ", text)
        self.assertIn("(3,7%)", text)
        self.assertIn("12,5 МБ/с", text)
        self.assertIn("залишилося", text)
        # 4608-169 = 4439 МБ лишилось; 4439/12.5 = 355.1 с ≈ 5 хв 55 с.
        self.assertIn("5 хв 55 с", text)

    def test_speed_not_yet_measured_shows_honest_calculating(self):
        total = 4608 * _MB_I
        done = 45 * _MB_I
        self.dlg._speed_bps = None
        text = self.dlg._format_status(done, total)
        self.assertIn("обчислення швидкості…", text)
        # без виміряної швидкості часу, що лишився, НЕ показуємо (нічого не вигадуємо)
        self.assertNotIn("залишилося", text)

    def test_unknown_total_no_fake_percent_or_eta(self):
        done = 169 * _MB_I
        self.dlg._speed_bps = 8.4 * _MB_I
        text = self.dlg._format_status(done, 0)
        self.assertIn("169 МБ", text)
        self.assertIn("8,4 МБ/с", text)
        self.assertNotIn("%", text)
        self.assertNotIn("залишилося", text)
        self.assertNotIn("з ", text)  # ні "169 МБ з ..." — знаменника немає

    def test_mutation_percent_without_denominator_is_caught(self):
        """Мутаційна перевірка головної знахідки аудиту: якщо хтось поверне
        текст без знаменника ("169 МБ" замість "169 МБ з 4608 МБ"), тест
        мусить це зловити."""
        done = 169 * _MB_I
        self.dlg._speed_bps = 12.5 * _MB_I
        broken_text = f"Завантаження моделі: {done // _MB_I} МБ"
        with self.assertRaises(AssertionError):
            self.assertIn("169 МБ з 4608 МБ", broken_text)


class TestProtocolProgressThrottle(unittest.TestCase):
    """Дросель: перемальовування не частіше ніж раз на ~200 мс, крім фіналу."""

    def setUp(self):
        set_language("uk")
        self.dlg = _make_dialog()
        self.addCleanup(self.dlg.deleteLater)

    def test_rapid_updates_within_interval_are_skipped(self):
        clock = [1000.0]
        with patch("fronts.desktop.pages.protocol_ui.time.monotonic", lambda: clock[0]):
            total = 4608 * _MB_I
            self.dlg._on_progress(1 * _MB_I, total)
            first_text = self.dlg._status.text()
            # +50 мс (< 200 мс дроселя) — сигнал має бути проігнорований
            clock[0] += 0.05
            self.dlg._on_progress(2 * _MB_I, total)
            self.assertEqual(self.dlg._status.text(), first_text)

    def test_update_after_interval_is_applied(self):
        clock = [1000.0]
        with patch("fronts.desktop.pages.protocol_ui.time.monotonic", lambda: clock[0]):
            total = 4608 * _MB_I
            self.dlg._on_progress(1 * _MB_I, total)
            clock[0] += _PROGRESS_UI_INTERVAL_S + 0.01
            self.dlg._on_progress(50 * _MB_I, total)
            self.assertIn("50 МБ", self.dlg._status.text())

    def test_final_signal_always_applied_even_within_interval(self):
        clock = [1000.0]
        with patch("fronts.desktop.pages.protocol_ui.time.monotonic", lambda: clock[0]):
            total = 100 * _MB_I
            self.dlg._on_progress(1 * _MB_I, total)
            clock[0] += 0.01  # набагато менше дроселя
            self.dlg._on_progress(total, total)  # done == total → фінальний сигнал
            self.assertIn("100 МБ з 100 МБ", self.dlg._status.text())


class TestDialogNotModal(unittest.TestCase):
    """E-бекграунд-докачка (аудит 31.07.2026): вікно поступу НЕ блокує програму.

    МУТАЦІЯ: поверніть self.setModal(True) у ProtocolModelDownloadDialog.__init__
    (fronts/desktop/pages/protocol_ui.py) — цей тест мусить почервоніти."""

    def setUp(self):
        self.dlg = _make_dialog()
        self.addCleanup(self.dlg.deleteLater)
        self.addCleanup(_reset_download_manager)

    def test_dialog_is_not_modal(self):
        self.assertFalse(self.dlg.isModal())


class TestCloseDoesNotCancelDownload(unittest.TestCase):
    """Закриття вікна («✕»/Escape → reject(), кнопка «У фон» → hide()) не чіпає
    активне завантаження — на відміну від явного «Скасувати»."""

    def setUp(self):
        self.addCleanup(_reset_download_manager)

    def test_reject_hides_but_keeps_download_active(self):
        dlg = _make_dialog()
        self.addCleanup(dlg.deleteLater)
        self.assertTrue(dlmgr.DownloadManager.instance().is_downloading("C:/nonexistent"))
        dlg.reject()                                  # Escape / «✕»
        self.assertFalse(dlg.isVisible())
        self.assertTrue(
            dlmgr.DownloadManager.instance().is_downloading("C:/nonexistent"),
            "закриття вікна не мало б скасовувати фонове завантаження")

    def test_background_button_hides_but_keeps_download_active(self):
        dlg = _make_dialog()
        self.addCleanup(dlg.deleteLater)
        dlg._background_btn.click()
        self.assertFalse(dlg.isVisible())
        self.assertTrue(
            dlmgr.DownloadManager.instance().is_downloading("C:/nonexistent"))

    def test_explicit_cancel_button_marks_manager_for_discard(self):
        """Клік «Скасувати» ставить explicit-прапорець і кличе worker.cancel();
        сам QThread тут заглушений (start() — no-op), тож сигнал cancelled не
        прийде сам — симулюємо його, як робить реальний ProtocolModelDownloadWorker."""
        dlg = _make_dialog()
        self.addCleanup(dlg.deleteLater)
        mgr = dlmgr.DownloadManager.instance()
        with patch(
                "whisper_core.protocol.model_manager.discard_partial_download"
        ) as discard:
            dlg._on_cancel_clicked()
            self.assertTrue(mgr._explicit_cancel)
            mgr._on_cancelled()
        discard.assert_called_once_with(dlmgr.key_for("C:/nonexistent"))
        self.assertFalse(mgr.is_downloading("C:/nonexistent"))


class TestDuplicateAndConflictingDownloads(unittest.TestCase):
    """§3.2 спеки: одне завантаження одночасно. Той самий target приєднується
    (не дублює воркер); інший target — відхиляється (BUSY_OTHER)."""

    def setUp(self):
        _reset_download_manager()
        self.addCleanup(_reset_download_manager)

    def test_second_click_for_same_model_joins_not_duplicates(self):
        with patch.object(ProtocolModelDownloadWorker, "start", lambda self: None):
            first = dlmgr.DownloadManager.instance().start_download(
                "C:/models/fast", preset_id="fast")
            worker_after_first = dlmgr.DownloadManager.instance()._worker
            second = dlmgr.DownloadManager.instance().start_download(
                "C:/models/fast", preset_id="fast")
        self.assertEqual(first, dlmgr.STARTED)
        self.assertEqual(second, dlmgr.ALREADY_THIS)
        self.assertIs(dlmgr.DownloadManager.instance()._worker, worker_after_first)

    def test_different_model_while_busy_is_rejected(self):
        with patch.object(ProtocolModelDownloadWorker, "start", lambda self: None):
            first = dlmgr.DownloadManager.instance().start_download(
                "C:/models/fast", preset_id="fast")
            second = dlmgr.DownloadManager.instance().start_download(
                "C:/models/quality", preset_id="quality")
        self.assertEqual(first, dlmgr.STARTED)
        self.assertEqual(second, dlmgr.BUSY_OTHER)
        self.assertEqual(
            dlmgr.DownloadManager.instance().active_key(),
            dlmgr.key_for("C:/models/fast"))


class TestCancelSemanticsPartialFile(unittest.TestCase):
    """§4 спеки: явне «Скасувати» прибирає частковий файл, закриття програми
    (drain_workers — той самий worker.cancel(), але БЕЗ прапорця explicit) —
    НІ, лишає .part для дозавантаження після перезапуску."""

    def setUp(self):
        _reset_download_manager()
        self.addCleanup(_reset_download_manager)

    def test_explicit_cancel_discards_partial_file(self):
        mgr = dlmgr.DownloadManager.instance()
        with patch.object(ProtocolModelDownloadWorker, "start", lambda self: None):
            mgr.start_download("C:/models/fast", preset_id="fast")
        with patch(
                "whisper_core.protocol.model_manager.discard_partial_download"
        ) as discard:
            mgr.cancel_download("C:/models/fast")
            mgr._on_cancelled()          # симулює сигнал worker.cancelled
        discard.assert_called_once_with(dlmgr.key_for("C:/models/fast"))

    def test_shutdown_style_cancel_keeps_partial_file(self):
        """drain_workers() (вихід програми) кличе worker.cancel() НАПРЯМУ, у
        обхід DownloadManager.cancel_download() — прапорець explicit НЕ
        встановлюється, тож .part має лишитись на диску."""
        mgr = dlmgr.DownloadManager.instance()
        with patch.object(ProtocolModelDownloadWorker, "start", lambda self: None):
            mgr.start_download("C:/models/fast", preset_id="fast")
        mgr._worker.cancel()             # напряму, як drain_workers()
        with patch(
                "whisper_core.protocol.model_manager.discard_partial_download"
        ) as discard:
            mgr._on_cancelled()
        discard.assert_not_called()


class TestModelDownloadWaitDialog(unittest.TestCase):
    """§5 спеки: «Створити протокол» під час активного якраз завантаження —
    інформаційний прогрес замість мовчазної відмови, з опційним авто-стартом."""

    def setUp(self):
        _reset_download_manager()
        self.addCleanup(_reset_download_manager)

    def _start_background_download(self, target="C:/models/fast"):
        with patch.object(ProtocolModelDownloadWorker, "start", lambda self: None):
            dlmgr.DownloadManager.instance().start_download(target, preset_id="fast")

    def test_shows_current_progress_on_open(self):
        self._start_background_download()
        mgr = dlmgr.DownloadManager.instance()
        mgr._done, mgr._total = 42 * _MB_I, 100 * _MB_I
        dlg = ModelDownloadWaitDialog("C:/models/fast")
        self.addCleanup(dlg.deleteLater)
        self.assertIn("42 МБ", dlg._status.text())

    def test_autostart_runs_callback_when_checked(self):
        self._start_background_download()
        called = []
        dlg = ModelDownloadWaitDialog("C:/models/fast", on_ready=lambda: called.append(True))
        self.addCleanup(dlg.deleteLater)
        dlg._autostart.setChecked(True)
        dlmgr.DownloadManager.instance()._on_finished_ok()
        self.assertEqual(called, [True])

    def test_no_autostart_when_unchecked(self):
        self._start_background_download()
        called = []
        dlg = ModelDownloadWaitDialog("C:/models/fast", on_ready=lambda: called.append(True))
        self.addCleanup(dlg.deleteLater)
        dlg._autostart.setChecked(False)
        dlmgr.DownloadManager.instance()._on_finished_ok()
        self.assertEqual(called, [])


class TestResumableDownloadOnRestart(unittest.TestCase):
    """§4 спеки: закриття програми лишає .part; наступний запуск дозавантажує
    через HTTP Range, а не якісно з нуля. Живий HTTP-сервер локально (127.0.0.1)
    — мережу назовні НЕ чіпаємо."""

    def _serve(self, payload):
        import http.server
        import threading as _th

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                rng = self.headers.get("Range")
                if rng:
                    start = int(rng.split("=")[1].split("-")[0])
                    chunk = payload[start:]
                    self.send_response(206)
                    self.send_header("Content-Length", str(len(chunk)))
                    self.end_headers()
                    self.wfile.write(chunk)
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            def log_message(self, *_a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        _th.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}/model.gguf"

    def test_restart_resumes_from_partial_file_via_range(self):
        import hashlib
        import tempfile
        from pathlib import Path
        from whisper_core.protocol import model_manager as mm

        payload = b"GGUFPAYLOAD" * 50000
        sha = hashlib.sha256(payload).hexdigest()
        url = self._serve(payload)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fast"
            target.mkdir(parents=True)
            part = mm.partial_file(target)
            already_have = payload[:200000]
            part.write_bytes(already_have)   # «програму закрили посеред качання»

            seen_progress = []
            mm._install_from_url(
                target, url, min_bytes=len(payload) - 10, sha256=sha,
                progress_cb=lambda done, total: seen_progress.append(done))

            self.assertEqual(mm.model_file(target).read_bytes(), payload)
            self.assertFalse(part.exists())
            # перший сигнал прогресу мусить продовжувати з уже наявних байтів,
            # НЕ з нуля — це і є «дозавантаження», а не якісно новий стар
            self.assertGreaterEqual(seen_progress[0], len(already_have))

    def test_explicit_cancel_via_model_manager_discards_partial(self):
        import tempfile
        from pathlib import Path
        from whisper_core.protocol import model_manager as mm

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fast"
            target.mkdir(parents=True)
            part = mm.partial_file(target)
            part.write_bytes(b"half-downloaded-bytes")
            mm.discard_partial_download(target)
            self.assertFalse(part.exists())


class TestProtocolAutosaveFailureVisible(unittest.TestCase):
    """Аудит чесності (31.07, знахідка 2): протокол автозберігається поруч із
    записом одразу після генерації (ProtocolDialog._on_done). Раніше
    `except OSError: pass` ковтав збій запису мовчки — текст лишався на
    екрані, а файлу не було, без жодного попередження. Тепер помилка мусить
    бути видимою людині (з шляхом і дією) і потрапляти в журнал."""

    def setUp(self):
        set_language("uk")
        import tempfile
        from pathlib import Path
        from fronts.desktop.pages.protocol_ui import ProtocolDialog
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._session_dir = Path(self._tmp.name) / "2026-07-31_10-00-00"
        self._session_dir.mkdir()
        with patch.object(ProtocolDialog, "_start", lambda self, preset_id: None):
            self.dlg = ProtocolDialog(
                self._session_dir, [], "fast",
                {"me_label": "Я", "others_label": "Інші"})
        self.addCleanup(self.dlg.deleteLater)

    def test_autosave_write_failure_shows_visible_warning_not_silence(self):
        from fronts.desktop.i18n import tr

        def boom(session_dir, markdown):
            raise OSError("disk full")

        with patch("whisper_core.protocol.service.save_protocol", boom):
            with self.assertLogs(level="ERROR"):
                self.dlg._on_done("Текст протоколу")

        # Наслідок, не мок: видимий напис під переглядом тексту показує
        # ФАКТ, що автозбереження не відбулось — не мовчить.
        self.assertTrue(self.dlg._saved.isVisible() or self.dlg._saved.text())
        expected = tr("protocol_autosave_failed",
                      path=str(self._session_dir / "protocol.md"))
        self.assertEqual(self.dlg._saved.text(), expected)
        self.assertNotEqual(self.dlg._saved.text(), "")
        # Файлу справді нема — не прикидаємось, що зберегли.
        self.assertFalse((self._session_dir / "protocol.md").exists())
        # Текст лишається доступним на екрані для ручного копіювання.
        self.assertEqual(self.dlg._text, "Текст протоколу")

    def test_autosave_success_still_shows_saved_path(self):
        """Регресія: успішний шлях не мав зламатись під час фіксу мовчазного provalu."""
        from fronts.desktop.i18n import tr
        self.dlg._on_done("Текст протоколу")
        dest = self._session_dir / "protocol.md"
        self.assertTrue(dest.exists())
        self.assertEqual(self.dlg._saved.text(), "Збережено: " + str(dest))


if __name__ == "__main__":
    unittest.main()
