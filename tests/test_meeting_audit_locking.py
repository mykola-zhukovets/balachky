"""Audit append is serialized between real processes."""
from contextlib import contextmanager
import os
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pathlib import Path
import shutil
import uuid

from whisper_core.history import history_lock
from whisper_core.meeting import audit_log


_EVENTS_PER_PROCESS = 20
_RACE_PAYLOAD = "x" * (128 * 1024)


@contextmanager
def _workspace_temp_dir(prefix):
    path = Path.cwd() / f"{prefix}{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _append_events(session_dir, worker_id):
    session_dir = Path(session_dir)
    sync_dir = session_dir / "test-sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    for index in range(_EVENTS_PER_PROCESS):
        own_ready = sync_dir / f"{index}-{worker_id}.ready"
        peer_ready = sync_dir / f"{index}-{1 - worker_id}.ready"
        own_ready.write_text("", encoding="utf-8")
        deadline = time.monotonic() + 30
        while not peer_ready.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"worker {worker_id} timed out at event {index}")
            time.sleep(0.005)
        audit_log.append_event(
            session_dir,
            audit_log.EVENT_EDITED,
            note={
                "worker": worker_id,
                "index": index,
                "payload": _RACE_PAYLOAD,
            },
        )


def _append_after_signal(session_dir, ready_path, go_path):
    session_dir = Path(session_dir)
    ready_path = Path(ready_path)
    go_path = Path(go_path)
    ready_path.write_text("", encoding="utf-8")
    deadline = time.monotonic() + 30
    while not go_path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("delayed append timed out")
        time.sleep(0.005)
    try:
        audit_log.append_event(session_dir, audit_log.EVENT_STOPPED)
    except audit_log.AuditLogDeleted:
        print("deleted-barrier")
        return
    raise AssertionError("append unexpectedly recreated a deleted meeting")


class AuditLogInterprocessLockTests(unittest.TestCase):
    def test_two_processes_append_without_breaking_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "meeting"
            project_root = Path(__file__).resolve().parents[1]
            worker_env = os.environ.copy()
            worker_env["PYTHONPATH"] = os.pathsep.join(
                filter(None, [str(project_root), worker_env.get("PYTHONPATH")])
            )
            workers = [
                subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--audit-worker",
                        str(session_dir),
                        str(worker_id),
                    ],
                    cwd=project_root,
                    env=worker_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for worker_id in range(2)
            ]

            outputs = []
            try:
                for worker in workers:
                    outputs.append(worker.communicate(timeout=60))
            finally:
                for worker in workers:
                    if worker.poll() is None:
                        worker.kill()
                        worker.wait()
            for worker, (stdout, stderr) in zip(workers, outputs):
                self.assertEqual(worker.returncode, 0, stdout + stderr)

            events = audit_log.read_events(session_dir)
            expected_count = 2 * _EVENTS_PER_PROCESS
            self.assertEqual(len(events), expected_count)
            self.assertEqual(
                [event["seq"] for event in events],
                list(range(expected_count)),
            )
            self.assertEqual(
                {
                    (event["note"]["worker"], event["note"]["index"])
                    for event in events
                },
                {
                    (worker_id, index)
                    for worker_id in range(2)
                    for index in range(_EVENTS_PER_PROCESS)
                },
            )
            result = audit_log.verify_chain(session_dir)
            self.assertEqual(result.status, audit_log.STATUS_VERIFIED)
            self.assertEqual(result.event_count, expected_count)

    def test_occupied_lock_times_out_without_appending(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "meeting"
            path = session_dir / "audit.jsonl"
            session_dir.mkdir()
            path.with_name(path.name + ".lock").write_bytes(b"0")
            acquired = threading.Event()
            release = threading.Event()

            def hold_lock():
                with history_lock(path):
                    acquired.set()
                    release.wait(5)

            holder = threading.Thread(target=hold_lock)
            holder.start()
            self.assertTrue(acquired.wait(5))

            previous_timeout = audit_log._APPEND_LOCK_TIMEOUT_SECONDS
            audit_log._APPEND_LOCK_TIMEOUT_SECONDS = 0.05
            try:
                with self.assertRaises(TimeoutError):
                    audit_log.append_event(session_dir, audit_log.EVENT_CREATED)
            finally:
                audit_log._APPEND_LOCK_TIMEOUT_SECONDS = previous_timeout
                release.set()
                holder.join(5)

            self.assertFalse(holder.is_alive())
            self.assertEqual(audit_log.read_events(session_dir), [])

    def test_desktop_audit_lock_timeouts_are_fast_and_visible(self):
        from fronts.desktop import app

        for lock_kind in ("journal", "barrier"):
            with self.subTest(lock_kind=lock_kind), \
                    _workspace_temp_dir(
                        f".audit-desktop-{lock_kind}-timeout-") as root:
                session_dir = root / "meeting"
                path = session_dir / "audit.jsonl"
                session_dir.mkdir()
                lock_path = (
                    path
                    if lock_kind == "journal"
                    else audit_log._barrier_lock_path(session_dir)
                )
                lock_path.with_name(lock_path.name + ".lock").write_bytes(b"0")
                acquired = threading.Event()
                release = threading.Event()
                notifications = []

                def hold_lock():
                    with history_lock(lock_path):
                        acquired.set()
                        release.wait(15)

                holder = threading.Thread(target=hold_lock)
                holder.start()
                self.assertTrue(acquired.wait(5))
                app._set_audit_corrupt_notifier(notifications.append)
                if hasattr(app, "_audit_timeout_warned_sessions"):
                    app._audit_timeout_warned_sessions.clear()
                try:
                    started = time.monotonic()
                    app._audit_event(session_dir, audit_log.EVENT_CREATED)
                    elapsed = time.monotonic() - started
                finally:
                    app._set_audit_corrupt_notifier(None)
                    release.set()
                    holder.join(5)

                self.assertFalse(holder.is_alive())
                self.assertLess(elapsed, 2.5)
                self.assertEqual(len(notifications), 1)
                self.assertIn("журнал", notifications[0].lower())
                self.assertEqual(audit_log.read_events(session_dir), [])

    def test_delayed_process_append_refuses_after_delete_without_resurrection(self):
        with _workspace_temp_dir(".audit-delete-") as root:
            session_dir = root / "2026-07-15_10-00-00"
            audit_log.append_event(session_dir, audit_log.EVENT_CREATED)
            ready = root / "ready"
            go = root / "go"
            project_root = Path(__file__).resolve().parents[1]
            worker_env = os.environ.copy()
            worker_env["PYTHONPATH"] = os.pathsep.join(
                filter(None, [str(project_root), worker_env.get("PYTHONPATH")])
            )
            worker = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--delayed-append-worker",
                    str(session_dir),
                    str(ready),
                    str(go),
                ],
                cwd=project_root,
                env=worker_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 10
                while not ready.exists():
                    if worker.poll() is not None:
                        stdout, stderr = worker.communicate()
                        self.fail(stdout + stderr)
                    if time.monotonic() >= deadline:
                        self.fail("delayed append worker did not become ready")
                    time.sleep(0.005)

                from fronts.desktop.app import DesktopApp
                controller = SimpleNamespace(
                    profile=SimpleNamespace(),
                    _meetings_root=lambda: root,
                )
                with patch(
                        "whisper_core.meeting.voice_memory.delete_pending_centroids",
                        return_value=True):
                    failures = DesktopApp.delete_meeting(
                        controller, session_dir.name)
                self.assertEqual(failures, ())
                self.assertFalse(session_dir.exists())
                go.write_text("", encoding="utf-8")
                stdout, stderr = worker.communicate(timeout=30)
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait()

            self.assertEqual(worker.returncode, 0, stdout + stderr)
            self.assertIn("deleted-barrier", stdout)
            self.assertFalse(session_dir.exists())

    def test_new_meeting_with_same_folder_name_gets_a_new_journal(self):
        with _workspace_temp_dir(".audit-reused-name-") as root:
            session_dir = root / "2026-07-15_10-00-00"
            old_first = audit_log.append_event(
                session_dir,
                audit_log.EVENT_CREATED,
                ts=1.0,
                note={"meeting": "old"},
            )
            with audit_log.deletion_barrier(session_dir):
                shutil.rmtree(session_dir)

            session_dir.mkdir()
            new_first = audit_log.append_event(
                session_dir,
                audit_log.EVENT_CREATED,
                ts=2.0,
                note={"meeting": "new"},
            )

            self.assertNotEqual(new_first["hash"], old_first["hash"])
            self.assertEqual(new_first["seq"], 0)
            self.assertEqual(
                [event["type"] for event in audit_log.read_events(session_dir)],
                [audit_log.EVENT_CREATED],
            )
            self.assertTrue(
                audit_log._deleted_marker_path(session_dir).is_file())

    def test_restored_deleted_journal_identity_is_still_refused(self):
        with _workspace_temp_dir(".audit-restored-journal-") as root:
            session_dir = root / "2026-07-15_10-00-00"
            audit_log.append_event(
                session_dir, audit_log.EVENT_CREATED, ts=1.0)
            old_journal = (session_dir / "audit.jsonl").read_bytes()
            with audit_log.deletion_barrier(session_dir):
                shutil.rmtree(session_dir)

            session_dir.mkdir()
            (session_dir / "audit.jsonl").write_bytes(old_journal)

            with self.assertRaises(audit_log.AuditLogDeleted):
                audit_log.append_event(
                    session_dir, audit_log.EVENT_STOPPED, ts=2.0)

    def test_tombstone_retains_each_deleted_identity_after_name_reuse(self):
        with _workspace_temp_dir(".audit-redeleted-name-") as root:
            session_dir = root / "2026-07-15_10-00-00"
            audit_log.append_event(
                session_dir, audit_log.EVENT_CREATED, ts=1.0)
            oldest_journal = (session_dir / "audit.jsonl").read_bytes()
            with audit_log.deletion_barrier(session_dir):
                shutil.rmtree(session_dir)

            session_dir.mkdir()
            audit_log.append_event(
                session_dir, audit_log.EVENT_CREATED, ts=2.0)
            with audit_log.deletion_barrier(session_dir):
                shutil.rmtree(session_dir)

            session_dir.mkdir()
            (session_dir / "audit.jsonl").write_bytes(oldest_journal)
            with self.assertRaises(audit_log.AuditLogDeleted):
                audit_log.append_event(
                    session_dir, audit_log.EVENT_STOPPED, ts=3.0)


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--delayed-append-worker":
        _append_after_signal(sys.argv[2], sys.argv[3], sys.argv[4])
    elif len(sys.argv) == 4 and sys.argv[1] == "--audit-worker":
        _append_events(sys.argv[2], int(sys.argv[3]))
    else:
        unittest.main()
