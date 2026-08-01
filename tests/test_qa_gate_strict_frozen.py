"""Black-box contract tests for the strict frozen release QA runner."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "release_qa.ps1"
PWSH = shutil.which("pwsh") or shutil.which("powershell")
COMMIT = "1" * 40


@unittest.skipUnless(PWSH, "PowerShell is required for the release QA runner")
class StrictFrozenReleaseQaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.local_app_data = Path(self.temp.name) / "localappdata"
        self.tool_dir = Path(self.temp.name) / "tools"
        self.tool_dir.mkdir()
        self.python = Path(sys.executable).resolve()

        self._git("init", "-q")
        self._git("config", "user.email", "qa@example.invalid")
        self._git("config", "user.name", "QA fixture")
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-qm", "fixture")
        self.commit = self._git("rev-parse", "HEAD").stdout.strip()

        self.dist = self.repo / "dist" / "Balachky"
        self.dist.mkdir(parents=True)
        self._make_sleeping_executable(self.dist / "Balachky.exe")
        self._write_pyright()
        self.log = self.local_app_data / "Balachky" / "logs" / "balachky.log"
        self.log.parent.mkdir(parents=True)
        self.log.write_text("frozen fixture startup\n", encoding="utf-8")
        self.audit = self.repo / "qa-reports" / "frozen-audit.json"
        self.audit.parent.mkdir()
        self._write_audit()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            text=True,
            capture_output=True,
        )

    def _copy_system_executable(self, name: str, target: Path) -> None:
        source = Path(os.environ["SystemRoot"]) / "System32" / name
        self.assertTrue(source.is_file(), f"test prerequisite missing: {source}")
        shutil.copy2(source, target)

    def _make_sleeping_executable(self, target: Path) -> None:
        compiler = (
            Path(os.environ["SystemRoot"])
            / "Microsoft.NET"
            / "Framework64"
            / "v4.0.30319"
            / "csc.exe"
        )
        self.assertTrue(compiler.is_file(), f"test prerequisite missing: {compiler}")
        source = target.with_suffix(".cs")
        source.write_text(
            "using System.Threading; public static class Program { "
            "public static void Main() { Thread.Sleep(5000); } }",
            encoding="utf-8",
        )
        subprocess.run(
            [str(compiler), "/nologo", f"/out:{target}", str(source)],
            check=True,
            capture_output=True,
            text=True,
        )
        source.unlink()

    def _write_pyright(self, output: str = "") -> None:
        (self.tool_dir / "pyright.cmd").write_text(
            f"@echo off\r\n{output}\r\nexit /b 0\r\n", encoding="utf-8"
        )

    def _write_audit(
        self,
        *,
        status: str = "passed",
        isolation: str = "verified",
        warnings: list[str] | None = None,
    ) -> None:
        self.audit.write_text(
            json.dumps(
                {
                    "commit": self.commit,
                    "status": status,
                    "isolation": isolation,
                    "warnings": warnings or [],
                }
            ),
            encoding="utf-8",
        )

    def _run(self, expected: str | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["LOCALAPPDATA"] = str(self.local_app_data)
        env["PATH"] = f"{self.tool_dir}{os.pathsep}{env['PATH']}"
        return subprocess.run(
            [
                PWSH,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(RUNNER),
                "-RepoRoot",
                str(self.repo.resolve()),
                "-PythonPath",
                str(self.python),
                "-Frozen",
                "-StrictFrozen",
                "-ExpectedCommit",
                expected or self.commit,
            ],
            text=True,
            capture_output=True,
            env=env,
            timeout=20,
        )

    def assert_fails(self, needle: str) -> None:
        result = self._run()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(needle.lower(), (result.stdout + result.stderr).lower())

    def test_clean_fixture_passes(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("STRICT FROZEN QA PASS", result.stdout)

    def test_head_mismatch_fails_closed(self) -> None:
        result = self._run("0" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HEAD", result.stdout + result.stderr)

    def test_missing_exact_frozen_executable_fails_closed(self) -> None:
        (self.dist / "Balachky.exe").unlink()
        self.assert_fails("Balachky.exe")

    def test_missing_pyright_fails_closed(self) -> None:
        (self.tool_dir / "pyright.cmd").unlink()
        self.assert_fails("pyright")

    def test_pyright_warning_fails_closed(self) -> None:
        self._write_pyright("echo WARNING fixture")
        self.assert_fails("warning")

    def test_missing_log_fails_closed(self) -> None:
        self.log.unlink()
        self.assert_fails("log")

    def test_early_app_exit_fails_closed(self) -> None:
        self._copy_system_executable("whoami.exe", self.dist / "Balachky.exe")
        self.assert_fails("exited")

    def test_skipped_audit_fails_closed(self) -> None:
        self._write_audit(status="skipped")
        self.assert_fails("audit")

    def test_unproven_isolation_fails_closed(self) -> None:
        self._write_audit(isolation="unproven")
        self.assert_fails("isolation")

    def test_second_instance_fails_closed(self) -> None:
        other = subprocess.Popen([str(self.dist / "Balachky.exe")])
        try:
            self.assert_fails("second instance")
        finally:
            other.kill()
            other.wait(timeout=5)

    def test_audit_warning_fails_closed(self) -> None:
        self._write_audit(warnings=["manual review pending"])
        self.assert_fails("warning")


if __name__ == "__main__":
    unittest.main()
