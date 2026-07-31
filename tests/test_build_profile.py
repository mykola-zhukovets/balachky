"""Детермінований склад PyInstaller-збірки без запуску самого PyInstaller."""

import ast
import contextlib
import importlib
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "balachky.spec"
DEV_REQUIREMENTS_PATH = ROOT / "requirements.txt"
TTS_BUILD_REQUIREMENTS_PATH = ROOT / "requirements-tts-build.txt"
PROFILE_START = "# === BUILD PROFILE START ==="
PROFILE_END = "# === BUILD PROFILE END ==="


def _active_requirements(path: Path) -> list[str]:
    return [
        requirement
        for line in path.read_text(encoding="utf-8").splitlines()
        if (requirement := line.partition("#")[0].strip())
    ]


def _profile_source() -> str:
    spec = SPEC_PATH.read_text(encoding="utf-8")
    start = spec.index(PROFILE_START)
    end = spec.index(PROFILE_END, start) + len(PROFILE_END)
    return spec[start:end]


def _evaluate_profile(env=None, missing_module=None):
    namespace = {}
    output = io.StringIO()

    def import_module(name):
        if name == missing_module:
            raise ModuleNotFoundError(f"No module named {name!r}")
        return object()

    with patch.dict(os.environ, env or {}, clear=True), \
            patch.object(importlib, "import_module", side_effect=import_module), \
            contextlib.redirect_stdout(output):
        exec(compile(_profile_source(), "balachky.spec", "exec"), namespace)
    return namespace, output.getvalue()


class BuildProfileTests(unittest.TestCase):
    def test_shared_dev_requirements_do_not_install_llama_cpp_python(self):
        active_requirements = _active_requirements(DEV_REQUIREMENTS_PATH)
        self.assertFalse(
            any(
                requirement.lower().startswith("llama-cpp-python")
                for requirement in active_requirements
            ),
            "llama-cpp-python must stay out of the shared dev/frozen venv",
        )

    def test_tts_build_requirements_pin_llama_cpp_python(self):
        self.assertIn(
            "llama-cpp-python==0.3.34",
            _active_requirements(TTS_BUILD_REQUIREMENTS_PATH),
        )

    def test_spec_does_not_call_find_spec(self):
        tree = ast.parse(SPEC_PATH.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "find_spec"
        ]
        self.assertEqual(
            calls,
            [],
            "склад збірки знову залежить від find_spec/локального venv",
        )

    def test_unset_profile_has_deterministic_no_tts_default(self):
        namespace, output = _evaluate_profile()
        self.assertEqual(namespace["_DEFAULT_BUILD_PROFILE"], "no-tts")
        self.assertEqual(namespace["_build_profile"], "no-tts")
        self.assertEqual(
            namespace["_build_components"],
            ("desktop", "diarization", "protocol"),
        )
        self.assertIn("BALACHKY BUILD PROFILE: no-tts", output)

    def test_full_profile_has_deterministic_component_list(self):
        namespace, output = _evaluate_profile(
            {"BALACHKY_BUILD_PROFILE": "full"}
        )
        self.assertEqual(namespace["_build_profile"], "full")
        self.assertEqual(
            namespace["_build_components"],
            ("desktop", "diarization", "protocol", "tts"),
        )
        self.assertIn("BALACHKY BUILD PROFILE: full", output)

    def test_unknown_profile_fails_with_allowed_values(self):
        with self.assertRaises(SystemExit) as caught:
            _evaluate_profile({"BALACHKY_BUILD_PROFILE": "ful"})
        message = str(caught.exception)
        self.assertIn("ful", message)
        self.assertIn("full", message)
        self.assertIn("no-tts", message)

    def test_empty_explicit_profile_is_not_treated_as_unset(self):
        with self.assertRaises(SystemExit) as caught:
            _evaluate_profile({"BALACHKY_BUILD_PROFILE": ""})
        self.assertIn("allowed values: full, no-tts", str(caught.exception))

    def test_legacy_skip_one_maps_to_no_tts_and_warns(self):
        namespace, output = _evaluate_profile({"BALACHKY_SKIP_TTS": "1"})
        self.assertEqual(namespace["_build_profile"], "no-tts")
        self.assertIn("BALACHKY_SKIP_TTS is deprecated", output)

    def test_legacy_skip_zero_maps_to_full_and_warns(self):
        namespace, output = _evaluate_profile({"BALACHKY_SKIP_TTS": "0"})
        self.assertEqual(namespace["_build_profile"], "full")
        self.assertIn("BALACHKY_SKIP_TTS is deprecated", output)

    def test_conflicting_new_and_legacy_profiles_fail(self):
        with self.assertRaises(SystemExit) as caught:
            _evaluate_profile(
                {
                    "BALACHKY_BUILD_PROFILE": "full",
                    "BALACHKY_SKIP_TTS": "1",
                }
            )
        self.assertIn("conflict", str(caught.exception).lower())

    def test_full_profile_without_torch_fails_clearly(self):
        with self.assertRaises(SystemExit) as caught:
            _evaluate_profile(
                {"BALACHKY_BUILD_PROFILE": "full"},
                missing_module="torch",
            )
        message = str(caught.exception)
        self.assertIn("full", message)
        self.assertIn("torch", message)

    def test_no_tts_profile_without_required_protocol_fails_clearly(self):
        with self.assertRaises(SystemExit) as caught:
            _evaluate_profile(missing_module="llama_cpp")
        message = str(caught.exception)
        self.assertIn("no-tts", message)
        self.assertIn("protocol", message)
        self.assertIn("llama_cpp", message)


if __name__ == "__main__":
    unittest.main()
