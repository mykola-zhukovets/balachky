import importlib.util
import json
import shutil
import threading
import time
import unittest
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fronts.desktop import app as desktop_app
from whisper_core.meeting import audit_log, signing, storage_crypto


_WORKTREE_TMP = Path(__file__).resolve().parents[1] / "dev" / "post85-test-work"


@contextmanager
def _temporary_directory():
    root = (_WORKTREE_TMP / uuid.uuid4().hex).resolve()
    if not root.is_relative_to(_WORKTREE_TMP.resolve()):
        raise RuntimeError("test path escaped the worktree")
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root)


class DesktopMeetingSigningTests(unittest.TestCase):
    def setUp(self):
        self.audit_warnings = []
        desktop_app._set_audit_corrupt_notifier(self.audit_warnings.append)
        desktop_app._audit_corrupt_warned_sessions.clear()
        if hasattr(desktop_app, "_audit_signing_warned_sessions"):
            desktop_app._audit_signing_warned_sessions.clear()
        if hasattr(desktop_app, "_audit_signing_rotation_warned_roots"):
            desktop_app._audit_signing_rotation_warned_roots.clear()
        self.dpapi = [
            patch.object(storage_crypto, "_dpapi_protect",
                         side_effect=lambda data: data),
            patch.object(storage_crypto, "_dpapi_unprotect",
                         side_effect=lambda data: data),
        ]
        for item in self.dpapi:
            item.start()

    def tearDown(self):
        for item in reversed(self.dpapi):
            item.stop()
        desktop_app._set_audit_corrupt_notifier(None)

    def test_desktop_append_passes_signer_and_requires_signature(self):
        signer = object()
        with _temporary_directory() as root:
            session = root / "meeting"
            with patch.object(
                    signing, "ensure_signing_identity",
                    return_value=signer) as ensure, patch.object(
                        audit_log, "append_event") as append:
                desktop_app._audit_event(session, audit_log.EVENT_CREATED)

        ensure.assert_called_once_with(root)
        append.assert_called_once_with(
            session,
            audit_log.EVENT_CREATED,
            signer=signer,
            require_signature=True,
            lock_timeout=desktop_app._AUDIT_DESKTOP_LOCK_TIMEOUT_SECONDS,
        )

    def test_first_meeting_creates_key_once_and_reuses_it(self):
        with _temporary_directory() as root:
            session = root / "meeting"

            desktop_app._audit_event(session, audit_log.EVENT_CREATED)
            key_path = root / ".audit-signing-key.json"
            first_container = key_path.read_bytes()
            desktop_app._audit_event(session, audit_log.EVENT_STOPPED)

            events = audit_log.read_events(session)
            self.assertEqual(len(events), 2)
            self.assertEqual(
                events[0]["auth"]["key_id"], events[1]["auth"]["key_id"])
            self.assertEqual(key_path.read_bytes(), first_container)
            result = audit_log.verify_chain(session)
            self.assertEqual(result.status, audit_log.STATUS_VERIFIED)
            self.assertEqual(result.auth_status, "signed_valid")

    def test_concurrent_first_meetings_generate_one_identity(self):
        with _temporary_directory() as root:
            barrier = threading.Barrier(2)
            create_lock = threading.Lock()
            create_calls = 0
            original_create = signing._create_signing_identity

            def counted_create(path):
                nonlocal create_calls
                with create_lock:
                    create_calls += 1
                time.sleep(0.05)
                return original_create(path)

            def ensure():
                barrier.wait()
                return signing.ensure_signing_identity(root)

            with patch.object(
                    signing, "_create_signing_identity",
                    side_effect=counted_create), ThreadPoolExecutor(
                        max_workers=2) as pool:
                identities = list(pool.map(lambda _item: ensure(), range(2)))

            self.assertEqual(create_calls, 1)
            self.assertEqual(identities[0].key_id, identities[1].key_id)
            self.assertEqual(
                identities[0].key_id,
                signing.load_signing_identity(root).key_id,
            )

    def test_signature_required_rejects_event_without_signer(self):
        with _temporary_directory() as root:
            with self.assertRaises(ValueError):
                audit_log.append_event(
                    root / "meeting",
                    audit_log.EVENT_CREATED,
                    require_signature=True,
                )

    def test_existing_unsigned_journal_continues_as_legacy(self):
        with _temporary_directory() as root:
            session = root / "meeting"
            audit_log.append_event(session, audit_log.EVENT_CREATED)

            desktop_app._audit_event(session, audit_log.EVENT_STOPPED)

            events = audit_log.read_events(session)
            self.assertNotIn("auth", events[0])
            self.assertNotIn("auth", events[1])
            result = audit_log.verify_chain(session)
            self.assertEqual(result.status, audit_log.STATUS_VERIFIED)
            self.assertEqual(result.auth_status, "unsigned_legacy")

    def test_finish_meeting_finalizes_signed_journal_with_artifact_hashes(self):
        from whisper_core.meeting import postprocess

        class Signal:
            def __init__(self):
                self.emits = []

            def emit(self, *args):
                self.emits.append(args)

        with _temporary_directory() as root:
            session = root / "meeting"
            session.mkdir()
            (session / "mic.wav").write_bytes(b"RIFF-audio")
            desktop_app._audit_event(session, audit_log.EVENT_CREATED)
            done_signal = Signal()
            controller = SimpleNamespace(
                cfg=SimpleNamespace(diarization_enabled=False),
                _meeting_pending={
                    "meeting": {
                        "session": None,
                        "dir": session,
                        "expected": 1,
                        "tracks": {"mic": ("text", [])},
                    },
                },
                _meeting_postprocessing={"meeting"},
                meeting_session_done=done_signal,
                meeting_state=Signal(),
                meeting_error=Signal(),
                _auto_obsidian=lambda _session_id: None,
            )

            def write_transcript(session_dir, _utterances, **_kwargs):
                (session_dir / "transcript.txt").write_text(
                    "final transcript", encoding="utf-8")
                (session_dir / "transcript.json").write_text(
                    "[]", encoding="utf-8")

            with patch.object(
                    postprocess, "stitch_tracks", return_value=[]), patch.object(
                        postprocess, "write_transcript",
                        side_effect=write_transcript), patch.object(
                            desktop_app, "_finalize_meeting_status",
                            return_value=SimpleNamespace(
                                id="meeting", status="done")), patch.object(
                                    desktop_app, "diagnostic_event"):
                desktop_app.DesktopApp._finish_meeting(controller, "meeting")

            events = audit_log.read_events(session)
            self.assertEqual([event["type"] for event in events],
                             [audit_log.EVENT_CREATED,
                              audit_log.EVENT_FINALIZED])
            finalized = events[-1]
            self.assertIsInstance(finalized.get("auth"), dict)
            self.assertEqual(
                set(finalized["artifacts"]),
                {"mic.wav", "transcript.txt", "transcript.json"},
            )
            result = audit_log.verify_chain(session)
            self.assertEqual(result.status, audit_log.STATUS_VERIFIED)
            self.assertEqual(result.auth_status, "signed_valid")

    def test_stripping_all_auth_is_signed_stripped_not_unsigned_legacy(self):
        with _temporary_directory() as root:
            session = root / "meeting"
            desktop_app._audit_event(session, audit_log.EVENT_CREATED)
            desktop_app._audit_event(session, audit_log.EVENT_STOPPED)
            log = session / "audit.jsonl"
            events = [
                json.loads(line)
                for line in log.read_text(encoding="utf-8").splitlines()
            ]
            for event in events:
                event.pop("auth", None)
            log.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False)
                          for event in events) + "\n",
                encoding="utf-8",
            )

            result = audit_log.verify_chain(session)

            self.assertEqual(events[0].get("signature_policy"), "required")
            self.assertEqual(result.status, audit_log.STATUS_BROKEN)
            self.assertEqual(result.auth_status, "signed_stripped")
            self.assertIn("signed_stripped", result.parse_error)

    def test_mixed_auth_legacy_signed_journal_refuses_desktop_append(self):
        with _temporary_directory() as root:
            session = root / "meeting"
            signer = signing.ensure_signing_identity(root)
            audit_log.append_event(
                session, audit_log.EVENT_CREATED,
                signer=signer, require_signature=True)
            audit_log.append_event(
                session, audit_log.EVENT_STOPPED, signer=signer)
            log = session / "audit.jsonl"
            events = [
                json.loads(line)
                for line in log.read_text(encoding="utf-8").splitlines()
            ]

            # Відтворити валідний signed-журнал старого формату без policy.
            log_id = events[0]["auth"]["log_id"]
            events[0].pop("signature_policy")
            prev = ""
            for event in events:
                event["prev"] = prev
                event["hash"] = audit_log._record_hash(
                    event["seq"], event["type"], event["ts"],
                    event.get("artifacts") or {}, event.get("note"), prev)
                event["auth"] = signing.sign_audit_record(
                    event, signer, log_id)
                prev = event["hash"]
            log.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False)
                          for event in events) + "\n",
                encoding="utf-8",
            )
            audit_log._write_head(session, events[-1])
            self.assertEqual(
                audit_log.verify_chain(session).auth_status, "signed_valid")

            # Атака: згасити signed-маркер лише в першій події.
            events[0].pop("auth")
            log.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False)
                          for event in events) + "\n",
                encoding="utf-8",
            )

            desktop_app._audit_event(session, audit_log.EVENT_STOPPED)

            after = audit_log.read_events(session)
            self.assertEqual(len(after), 2)
            self.assertEqual(len(self.audit_warnings), 1)
            result = audit_log.verify_chain(session)
            self.assertEqual(result.status, audit_log.STATUS_BROKEN)
            self.assertIn("mixed_auth_journal", result.parse_error)

    def test_deleted_key_between_events_refuses_append_and_warns(self):
        with _temporary_directory() as root:
            session = root / "meeting"
            desktop_app._audit_event(session, audit_log.EVENT_CREATED)
            old_key_id = audit_log.read_events(session)[0]["auth"]["key_id"]
            key_path = root / ".audit-signing-key.json"
            key_path.unlink()

            desktop_app._audit_event(session, audit_log.EVENT_STOPPED)

            replacement = signing.load_signing_identity(root)
            self.assertNotEqual(replacement.key_id, old_key_id)
            self.assertTrue(key_path.exists())
            self.assertEqual(len(audit_log.read_events(session)), 1)
            result = audit_log.verify_chain(session)
            self.assertEqual(result.status, audit_log.STATUS_VERIFIED)
            self.assertEqual(result.auth_status, "signed_valid")
            self.assertEqual(
                self.audit_warnings,
                [
                    desktop_app.tr("meeting_audit_signing_rotated_warn"),
                    desktop_app.tr("meeting_audit_signing_warn"),
                ],
            )
            with self.assertRaisesRegex(
                    signing.SigningKeyMissing,
                    "Активний ключ підпису не відповідає public key"):
                audit_log.append_event(
                    session, audit_log.EVENT_STOPPED, signer=replacement)
            self.assertEqual(len(audit_log.read_events(session)), 1)

    def test_deleted_key_rotates_for_new_meeting_and_warns(self):
        with _temporary_directory() as root:
            old_session = root / "old-meeting"
            desktop_app._audit_event(old_session, audit_log.EVENT_CREATED)
            old_key_id = audit_log.read_events(old_session)[0]["auth"]["key_id"]
            key_path = root / ".audit-signing-key.json"
            key_path.unlink()

            new_session = root / "new-meeting"
            desktop_app._audit_event(new_session, audit_log.EVENT_CREATED)

            events = audit_log.read_events(new_session)
            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0].get("auth"), dict)
            self.assertNotEqual(events[0]["auth"]["key_id"], old_key_id)
            self.assertTrue(key_path.exists())
            self.assertEqual(
                self.audit_warnings,
                [desktop_app.tr("meeting_audit_signing_rotated_warn")],
            )

    def test_signing_key_corrupt_warns_without_append(self):
        with _temporary_directory() as root:
            session = root / "meeting"
            with patch.object(
                    signing, "ensure_signing_identity",
                    side_effect=signing.SigningKeyCorrupt(
                        "corrupt signing key")), patch.object(
                            audit_log, "append_event") as append:
                desktop_app._audit_event(session, audit_log.EVENT_CREATED)

            append.assert_not_called()
            self.assertEqual(len(self.audit_warnings), 1)

    def test_mismatched_signer_is_rejected_before_append(self):
        with _temporary_directory() as base:
            root_a = base / "a"
            root_b = base / "b"
            root_a.mkdir()
            root_b.mkdir()
            signer_a = signing.ensure_signing_identity(root_a)
            signer_b = signing.ensure_signing_identity(root_b)
            session = root_a / "meeting"
            audit_log.append_event(
                session, audit_log.EVENT_CREATED,
                signer=signer_a, require_signature=True)

            with self.assertRaises(signing.SigningKeyMissing):
                audit_log.append_event(
                    session, audit_log.EVENT_STOPPED, signer=signer_b)

            self.assertEqual(len(audit_log.read_events(session)), 1)

    def test_mismatched_key_id_is_rejected_even_for_same_public_key(self):
        with _temporary_directory() as root:
            signer = signing.ensure_signing_identity(root)
            session = root / "meeting"
            audit_log.append_event(
                session, audit_log.EVENT_CREATED,
                signer=signer, require_signature=True)
            mismatched_key_id = "sha256:" + "0" * 64
            self.assertNotEqual(mismatched_key_id, signer.key_id)
            forged_identity = signing.SigningIdentity(
                key_id=mismatched_key_id,
                public_key_b64=signer.public_key_b64,
                _private_key=signer._private_key,
            )

            with self.assertRaises(signing.SigningKeyMissing):
                audit_log.append_event(
                    session, audit_log.EVENT_STOPPED,
                    signer=forged_identity)

            self.assertEqual(len(audit_log.read_events(session)), 1)

    def test_evidence_export_passes_signer(self):
        signer = object()
        package = object()
        with _temporary_directory() as root:
            session = root / "meeting"
            session.mkdir()
            controller = type(
                "Controller",
                (),
                {
                    "_meeting_session_dir": lambda self, _session_id: session,
                    "_meetings_root": lambda self: root,
                },
            )()
            with patch.object(
                    signing, "ensure_signing_identity",
                    return_value=signer), patch(
                        "whisper_core.meeting.evidence.export_evidence",
                        return_value=package) as export, patch.object(
                            desktop_app, "_audit_event"):
                result = desktop_app.DesktopApp.export_meeting_evidence(
                    controller, "meeting", root / "evidence.zip")

        self.assertIs(result, package)
        export.assert_called_once_with(
            session,
            root / "evidence.zip",
            app_version=desktop_app.DISPLAY_VERSION,
            signer=signer,
            meetings_root=root,
        )

    def test_desktop_signed_evidence_verifies_with_standalone_script(self):
        with _temporary_directory() as root:
            session = root / "meeting"
            desktop_app._audit_event(session, audit_log.EVENT_CREATED)
            key_id = audit_log.read_events(session)[0]["auth"]["key_id"]
            controller = type(
                "Controller",
                (),
                {
                    "_meeting_session_dir": lambda self, _session_id: session,
                    "_meetings_root": lambda self: root,
                },
            )()
            archive = root / "evidence.zip"

            desktop_app.DesktopApp.export_meeting_evidence(
                controller, "meeting", archive)
            extracted = root / "extracted"
            with zipfile.ZipFile(archive) as package:
                package.extractall(extracted)
            verify_path = (
                Path(__file__).resolve().parents[1] / "scripts" / "verify.py")
            spec = importlib.util.spec_from_file_location(
                "post85_standalone_verify", verify_path)
            verifier = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(verifier)

            status, _details = verifier.verify_evidence(
                extracted, expect_key_id=key_id)

            self.assertEqual(status, "verified")


if __name__ == "__main__":
    unittest.main()
