"""Юніти сторони запису наради (Б1) — session.py. Без Qt, без реального аудіо:
сегменти пишемо байтами у tempfile, вміст не важить (лічильники — за довжиною)."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from whisper_core.meeting import session
from whisper_core.meeting.session import (
    MeetingMeta, MeetingSession, create_session, delete_session, finalize_dir,
    find_orphans, list_sessions, load_meta, mark_interrupted,
)
from whisper_core.meeting import (
    PRESET_BOTH, PRESET_ONLYMIC, STATUS_DONE, STATUS_INTERRUPTED,
    STATUS_RECORDING, STATUS_STOPPED,
)


def _frames(n_frames: int, bytes_per_frame: int = 4) -> bytes:
    return b"\x00" * (n_frames * bytes_per_frame)


def _crashed_session(root, *, frames, rate=100, channels=1,
                     sid="2026-07-15_14-30-05"):
    """Осиротіла сесія на диску: сегмент записано, meeting.json = recording,
    дескриптори «закриті ОС» (як після краху процесу, коли recovery робить уже
    НОВИЙ процес). Без живого MeetingSession — жодного відкритого файлу в тесті."""
    d = Path(root) / sid
    (d / "mic").mkdir(parents=True)
    (d / "mic" / "0000.f32").write_bytes(b"\x00" * frames * channels * 4)
    meta = MeetingMeta(schema=1, id=sid, created=1, status=STATUS_RECORDING,
                       preset=PRESET_ONLYMIC, sources=["mic"], rate=rate,
                       channels=channels)
    (d / "meeting.json").write_text(meta.to_json(), encoding="utf-8")
    return d


class SessionDirTests(unittest.TestCase):
    def test_dir_name_is_start_time_and_meta_marks_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = MeetingSession(Path(tmp), ["mic"])
            self.assertTrue(s.dir.is_dir())
            self.assertEqual(s.id, s.dir.name)
            meta = load_meta(s.dir)
            self.assertEqual(meta.status, STATUS_RECORDING)   # ще не фіналізована
            self.assertEqual(meta.preset, PRESET_ONLYMIC)
            self.assertEqual(meta.sources, ["mic"])

    def test_two_starts_same_second_get_numeric_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(session.time, "strftime",
                              return_value="2026-07-15_14-30-05"):
                a = MeetingSession(Path(tmp), ["mic"])
                b = MeetingSession(Path(tmp), ["mic"])
            self.assertEqual(a.id, "2026-07-15_14-30-05")
            self.assertEqual(b.id, "2026-07-15_14-30-05-1")

    def test_both_preset_derives_from_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = MeetingSession(Path(tmp), ["mic", "sys"])
            self.assertEqual(load_meta(s.dir).preset, PRESET_BOTH)


class SegmentRotationTests(unittest.TestCase):
    def test_segments_rotate_and_preserve_all_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            # маленькі числа замість 45 с × 48 кГц: сегмент = 10 кадрів
            s = MeetingSession(Path(tmp), ["mic"], rate=10, channels=1,
                               segment_seconds=1)
            block = _frames(4)               # 4 кадри < сегмент → ротація природна
            for _ in range(7):               # 28 кадрів → мінімум 2 повні сегменти
                s.mic_sink(block)
            s.finalize()
            segs = sorted((s.dir / "mic").glob("*.f32"))
            self.assertGreaterEqual(len(segs), 2)
            self.assertEqual([p.name for p in segs[:2]], ["0000.f32", "0001.f32"])
            total = sum(p.stat().st_size for p in segs)
            self.assertEqual(total, 28 * 4)   # жоден байт не загублено
            self.assertFalse((s.dir / "sys").exists())   # непрописана доріжка → теки нема

    def test_concurrent_write_and_forced_rotation_lose_nothing(self):
        # гонка F2: watchdog (потік-монітор) форсує close_segment паралельно з
        # _write у read-потоці; без замка доріжки _write влучав у щойно закритий
        # файл (ValueError) і потік тихо вмирав. Із замком — жодного винятку,
        # кожен байт на диску
        with tempfile.TemporaryDirectory() as tmp:
            s = MeetingSession(Path(tmp), ["mic"], rate=1_000_000, channels=1,
                               segment_seconds=60)
            errors = []
            n_blocks, block = 3000, _frames(8)

            def writer():
                try:
                    for _ in range(n_blocks):
                        s.mic_sink(block)
                except Exception as exc:      # noqa: BLE001 - фіксуємо будь-який збій
                    errors.append(exc)

            t = threading.Thread(target=writer)
            t.start()
            while t.is_alive():               # молотимо ротацією, поки пише
                s.close_segment("mic")
            t.join()
            s.finalize()
            self.assertEqual(errors, [])
            total = sum(p.stat().st_size
                        for p in (s.dir / "mic").glob("*.f32"))
            self.assertEqual(total, n_blocks * len(block))

    def test_close_segment_forces_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = MeetingSession(Path(tmp), ["mic"], rate=1000, channels=1,
                               segment_seconds=60)
            s.mic_sink(_frames(5))
            s.close_segment("mic")           # watchdog: форсована ротація
            s.mic_sink(_frames(3))
            s.finalize()
            segs = sorted((s.dir / "mic").glob("*.f32"))
            self.assertEqual([p.name for p in segs], ["0000.f32", "0001.f32"])
            self.assertEqual(segs[0].stat().st_size, 5 * 4)
            self.assertEqual(segs[1].stat().st_size, 3 * 4)


class MetaTests(unittest.TestCase):
    def test_to_from_json_round_trip(self):
        meta = MeetingMeta(
            schema=1, id="2026-07-15_14-30-05", created=1768480205,
            status=STATUS_STOPPED, preset=PRESET_BOTH, sources=["mic", "sys"],
            mic_device="Мікрофон (Realtek)", sys_device="Динаміки (loopback)",
            duration=123.4, marks=[{"time": 5.0, "note": None}])
        again = MeetingMeta.from_json(meta.to_json())
        self.assertEqual(again, meta)

    def test_missing_reserved_fields_default(self):
        # стара сесія без protocol/marks читається без падіння (сумісність назад)
        text = ('{"schema":1,"id":"x","created":1,"status":"done",'
                '"preset":"onlymic","sources":["mic"]}')
        meta = MeetingMeta.from_json(text)
        self.assertIsNone(meta.protocol)
        self.assertEqual(meta.marks, [])

    def test_finalize_sets_status_and_duration_from_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = MeetingSession(Path(tmp), ["mic"], rate=100, channels=1,
                               segment_seconds=1)
            s.mic_sink(_frames(250))         # 250 кадрів / 100 Гц = 2.5 с
            meta = s.finalize(STATUS_STOPPED)
            self.assertEqual(meta.status, STATUS_STOPPED)
            self.assertEqual(meta.duration, 2.5)
            self.assertEqual(load_meta(s.dir).duration, 2.5)

    def test_finalize_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = MeetingSession(Path(tmp), ["mic"], rate=100, channels=1)
            s.mic_sink(_frames(100))
            first = s.finalize(STATUS_DONE)
            again = s.finalize(STATUS_STOPPED)   # другий виклик нічого не міняє
            self.assertEqual(again.status, STATUS_DONE)
            self.assertEqual(first.duration, again.duration)


class RegistryTests(unittest.TestCase):
    def test_list_sessions_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(session.time, "strftime",
                              return_value="2026-07-15_10-00-00"):
                old = MeetingSession(root, ["mic"]); old.finalize()
            with patch.object(session.time, "strftime",
                              return_value="2026-07-15_12-00-00"):
                new = MeetingSession(root, ["mic"]); new.finalize()
            ids = [m.id for m in list_sessions(root)]
            self.assertEqual(ids, [new.id, old.id])

    def test_find_orphans_only_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orphan = MeetingSession(root, ["mic"])          # лишається recording
            done = MeetingSession(root, ["mic"]); done.finalize(STATUS_DONE)
            orphan_ids = [m.id for m in find_orphans(root)]
            self.assertIn(orphan.id, orphan_ids)
            self.assertNotIn(done.id, orphan_ids)

    def test_stopped_postprocess_session_is_recovery_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = _crashed_session(tmp, frames=100, rate=100)
            finalize_dir(d, STATUS_STOPPED)  # crash після stop(), до postprocess
            orphan_ids = [m.id for m in find_orphans(root)]
            self.assertIn(d.name, orphan_ids)

    def test_mark_interrupted_transitions_and_measures(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _crashed_session(tmp, frames=100, rate=100)   # 100 кадрів / 100 Гц
            meta = mark_interrupted(d)
            self.assertEqual(meta.status, STATUS_INTERRUPTED)
            self.assertEqual(meta.duration, 1.0)             # дорахувано з диска
            self.assertEqual(load_meta(d).status, STATUS_INTERRUPTED)
            self.assertEqual(find_orphans(Path(tmp)), [])    # більше не осиротіла

    def test_mark_interrupted_missing_meta_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(mark_interrupted(Path(tmp) / "nope"))

    def test_finalize_dir_completes_recovered_session(self):
        # відновлення осиротілої (Б2): після успішної розшифровки статус на диску
        # стає завершеним, duration дораховано з файлів — без живого MeetingSession
        with tempfile.TemporaryDirectory() as tmp:
            d = _crashed_session(tmp, frames=250, rate=100)   # 2.5 с на диску
            meta = finalize_dir(d, STATUS_DONE)
            self.assertEqual(meta.status, STATUS_DONE)
            self.assertEqual(meta.duration, 2.5)
            again = load_meta(d)
            self.assertEqual(again.status, STATUS_DONE)
            self.assertEqual(again.duration, 2.5)

    def test_finalize_dir_default_status_is_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _crashed_session(tmp, frames=100, rate=100)
            self.assertEqual(finalize_dir(d).status, STATUS_STOPPED)

    def test_finalize_dir_missing_meta_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(finalize_dir(Path(tmp) / "nope"))


class StorageErrorPersistenceTests(unittest.TestCase):
    def test_storage_error_retries_after_next_successful_sink(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = MeetingSession(Path(tmp), ["mic"], rate=100, channels=1)
            real_write = session.atomic_write_json
            attempts = 0

            def no_space_once(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("ENOSPC")
                return real_write(*args, **kwargs)

            with patch.object(session, "atomic_write_json", side_effect=no_space_once):
                with self.assertRaises(OSError):
                    s.mark_storage_error("mic", 12.9)
                self.assertTrue(s._storage_error_persist_pending)
                s.mic_sink(_frames(1))

            self.assertFalse(s._storage_error_persist_pending)
            self.assertEqual(load_meta(s.dir).storage_error, {
                "kind": "disk_full", "track": "mic", "elapsed_seconds": 12,
            })
            s.finalize()  # закрити відкриті сегмент-файли до прибирання теки

    def test_storage_error_persists_by_releasing_reserve_on_full_disk(self):
        # С3: на «повному диску» meta-запис проходить з ПЕРШОЇ спроби лише тому,
        # що mark_storage_error спершу звільняє заздалегідь виділений резерв.
        with tempfile.TemporaryDirectory() as tmp:
            s = MeetingSession(Path(tmp), ["mic"], rate=100, channels=1)
            reserve = s.dir / session._RESERVE_NAME
            self.assertTrue(reserve.exists())
            real_write = session.atomic_write_json

            def full_until_reserve_freed(path, payload):
                if Path(path).name == "meeting.json" and reserve.exists():
                    raise OSError(28, "No space left on device")
                return real_write(path, payload)

            with patch.object(session, "atomic_write_json",
                              side_effect=full_until_reserve_freed):
                s.mark_storage_error("mic", 30)

            self.assertFalse(reserve.exists())
            self.assertFalse(s._storage_error_persist_pending)
            self.assertEqual(load_meta(s.dir).storage_error["track"], "mic")
            s.finalize()

    def test_finalize_releases_storage_reserve(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = MeetingSession(Path(tmp), ["mic"], rate=100, channels=1)
            reserve = s.dir / session._RESERVE_NAME
            self.assertTrue(reserve.exists())
            s.finalize()
            self.assertFalse(reserve.exists())


class DeleteTests(unittest.TestCase):
    def test_delete_removes_whole_dir_and_second_call_is_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = create_session(Path(tmp), ["mic", "sys"])
            s.mic_sink(_frames(10))
            (s.dir / "transcript.txt").write_text("текст", encoding="utf-8")
            s.finalize()
            d = s.dir
            self.assertTrue(delete_session(d))       # аудіо + транскрипт + meta разом
            self.assertFalse(d.exists())
            self.assertFalse(delete_session(d))      # повторний виклик — без винятку


class PathTraversalTests(unittest.TestCase):
    """R1: підкладена/пошкоджена сесія з meeting.json, де "id" містить traversal
    ("..\\інша-тека"), НЕ повинна дати видаленню вийти за межі сховища."""

    def test_list_sessions_id_is_disk_folder_not_json_field(self):
        # рубіж 1: тека з нормальною назвою, але json бреше id="..\\evil".
        # СТАРИЙ код повертав meta.id == "..\\evil" (і UI будував store/..\\evil);
        # новий — завжди назву теки на диску.
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store"; store.mkdir()
            d = store / "2026-07-15_14-30-05"; d.mkdir()
            evil = MeetingMeta(schema=1, id="..\\evil", created=1,
                               status=STATUS_STOPPED, preset=PRESET_ONLYMIC,
                               sources=["mic"])
            (d / "meeting.json").write_text(evil.to_json(), encoding="utf-8")
            [meta] = list_sessions(store)
            self.assertEqual(meta.id, "2026-07-15_14-30-05")   # НЕ "..\\evil"
            # шлях, який побудує UI з цього id, лишається у сховищі:
            self.assertTrue((store / meta.id).resolve().is_relative_to(store.resolve()))

    def test_delete_refuses_path_outside_store(self):
        # рубіж 3: навіть якщо шлях повз рубежі 1/2 (напр. id-виглядає-валідним,
        # але тека фізично поза сховищем) — realpath/commonpath ловить і відмовляє.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = base / "store"; store.mkdir()
            victim = base / "2026-01-01_00-00-00"; victim.mkdir()
            (victim / "important.txt").write_text("береже", encoding="utf-8")
            # store/..\\<victim> — назва проходить рубіж 2, але лежить ПОЗА store
            escaping = store / ".." / "2026-01-01_00-00-00"
            self.assertFalse(delete_session(escaping, root=store))   # відмова
            self.assertTrue(victim.exists())                         # ціле
            self.assertTrue((victim / "important.txt").exists())

    def test_delete_refuses_unsafe_basename(self):
        # рубіж 2: назва теки не у форматі create_session → відмова без rmtree
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store"; store.mkdir()
            weird = store / "evil"; weird.mkdir()
            (weird / "keep.txt").write_text("x", encoding="utf-8")
            self.assertFalse(delete_session(weird, root=store))
            self.assertTrue(weird.exists())

    def test_normal_session_still_deletes(self):
        # нормальна сесія видаляється як раніше (регресія рубежів 2/3)
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store"; store.mkdir()
            s = create_session(store, ["mic"]); s.finalize()
            self.assertTrue(delete_session(s.dir, root=store))
            self.assertFalse(s.dir.exists())


class AtomicWriteTests(unittest.TestCase):
    """E8: meeting.json пишеться атомарно (temp + os.replace). Крах ПІД ЧАС
    запису не повинен лишати биту/порожню meta — стара валідна цілою."""

    def test_atomic_write_json_crash_before_replace_keeps_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meeting.json"
            path.write_text('{"status":"recording"}', encoding="utf-8")
            with patch.object(session.os, "replace", side_effect=OSError("crash")):
                with self.assertRaises(OSError):
                    session.atomic_write_json(path, {"status": "done"})
            self.assertEqual(path.read_text(encoding="utf-8"), '{"status":"recording"}')

    def test_crash_before_replace_keeps_old_meta_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _crashed_session(tmp, frames=100, rate=100)   # валідна meta = recording
            before = (d / "meeting.json").read_text(encoding="utf-8")
            # мок помилки на моменті os.replace (temp уже записано, підміна не сталась)
            with patch.object(session.os, "replace", side_effect=OSError("crash")):
                with self.assertRaises(OSError):
                    finalize_dir(d, STATUS_DONE)              # пише meta → replace падає
            after = (d / "meeting.json").read_text(encoding="utf-8")
            self.assertEqual(after, before)                  # стара meta недоторкана
            self.assertEqual(load_meta(d).status, STATUS_RECORDING)   # не побита

    def test_atomic_write_retries_transient_permission_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meeting.json"
            real_replace = session.os.replace
            attempts = 0

            def sharing_violation_then_replace(src, dst):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    exc = PermissionError("sharing violation")
                    exc.winerror = 32
                    raise exc
                return real_replace(src, dst)

            with patch.object(session.os, "replace", side_effect=sharing_violation_then_replace), \
                 patch.object(session.time, "sleep") as sleep:
                session.atomic_write_json(path, {"status": "done"})

            self.assertEqual(attempts, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertIn('"status": "done"', path.read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
