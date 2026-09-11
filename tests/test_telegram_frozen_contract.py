"""Contract tests for packaging Telegram integration in the frozen distribution."""
import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "balachky.spec"


def _assigned_nodes(tree: ast.AST) -> dict[str, ast.AST]:
    result = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name):
            result[target.id] = node.value
    return result


def _string_values(node: ast.AST, assigned: dict[str, ast.AST]) -> set[str]:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values = set()
        for item in node.elts:
            values.update(_string_values(item, assigned))
        return values
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name):
        return _string_values(assigned[node.id], assigned)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return (
            _string_values(node.left, assigned)
            | _string_values(node.right, assigned)
        )
    raise AssertionError(f"unsupported excludes expression: {ast.dump(node)}")


def _analysis_excludes() -> list[set[str]]:
    tree = ast.parse(SPEC_PATH.read_text(encoding="utf-8"))
    assigned = _assigned_nodes(tree)
    result = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Analysis"
        ):
            continue
        keyword = next(
            (item for item in node.keywords if item.arg == "excludes"),
            None,
        )
        assert keyword is not None, "every Analysis must declare excludes"
        result.append(_string_values(keyword.value, assigned))
    return result


class TelegramFrozenContractTests(unittest.TestCase):
    def test_desktop_spec_no_longer_excludes_telegram_runtime(self):
        analyses = _analysis_excludes()
        self.assertEqual(len(analyses), 3, "GUI, TTS worker, and protocol worker expected")
        gui, tts_worker, protocol_worker = analyses
        self.assertNotIn("aiogram", gui)
        self.assertNotIn("fronts.telegram", gui)
        for worker in (tts_worker, protocol_worker):
            self.assertIn("aiogram", worker)
            self.assertIn("fronts.telegram", worker)

    def test_telegram_runtime_is_importable_without_token(self):
        env = dict(os.environ)
        env.pop("WHISPER_TYPER_BOT_TOKEN", None)
        env["PYTHONPATH"] = str(ROOT)
        proc = subprocess.run(
            [sys.executable, "-c", "import fronts.telegram.service; print('ok')"],
            env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
