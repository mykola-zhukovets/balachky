"""feature/voice-edit-selection — Command Mode: голосове редагування виділеного.

Три пласти:
  1. Ядро (command_edit.py) БЕЗ моделі: формування промту з виділеного тексту +
     команди, постобробка, гейт порожнечі — через mock-LLM.
  2. Оркестрація CommandEditGenerator через СПРАВЖНІЙ worker-підпроцес із
     фейк-бекендом: гейт «встановіть компонент» замість заглушки, гейт відсутньої
     моделі, скасування (той самий патерн, що RewriteGenerator/QAGenerator).
  3. Зчитування виділеного (paste.capture_selection): збереження й ВІДНОВЛЕННЯ
     буфера обміну, детекція «нічого не виділено».
"""
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.protocol import ENV_FAKE_BACKEND
from whisper_core.protocol import command_edit as ce
from whisper_core.protocol import model_manager as mm
from whisper_core.protocol.service import (
    CommandEditGenerator, CommandEditError, CommandEditCancelled,
    CommandEditModelMissing)

_WORKER = [sys.executable, "-m", "whisper_core.protocol.worker"]


# ─────────────────────────── 1. ядро: промт + конвеєр ───────────────────────
class PromptTests(unittest.TestCase):
    def test_prompt_contains_both_inputs(self):
        p = ce.build_command_prompt("Привіт, як справи", "зроби офіційніше")
        self.assertIn("Привіт, як справи", p)
        self.assertIn("зроби офіційніше", p)
        self.assertIn("Команда", p)
        self.assertIn("Фрагмент тексту", p)

    def test_pipeline_passes_prompt_and_returns_output(self):
        seen = {}

        def fake_llm(prompt, **kw):
            seen["prompt"] = prompt
            return "Доброго дня. Як ваші справи?"

        out = ce.apply_command("привіт як ти", "зроби офіційніше", fake_llm)
        self.assertEqual(out, "Доброго дня. Як ваші справи?")
        self.assertIn("привіт як ти", seen["prompt"])
        self.assertIn("зроби офіційніше", seen["prompt"])

    def test_pipeline_postprocesses_code_fence(self):
        out = ce.apply_command("текст", "скороти",
                               lambda p, **k: "```\nкоротко\n```")
        self.assertEqual(out, "коротко")

    def test_empty_selection_returns_empty_without_calling_model(self):
        called = []
        out = ce.apply_command("   ", "зроби офіційніше",
                               lambda p, **k: called.append(p) or "x")
        self.assertEqual(out, "")
        self.assertEqual(called, [])

    def test_empty_command_returns_empty_without_calling_model(self):
        called = []
        out = ce.apply_command("реальний текст", "   ",
                               lambda p, **k: called.append(p) or "x")
        self.assertEqual(out, "")
        self.assertEqual(called, [])


# ─────────────────────────── 2. оркестрація ─────────────────────────────────
def _make_ready_model(root: Path):
    d = root / "fast"
    d.mkdir(parents=True)
    (d / mm.MODEL_FILENAME).write_bytes(b"GGUF" + b"\x00" * 500)
    (d / mm._READY_MARKER).write_text("ok", encoding="utf-8")
    return d


class CommandEditGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cerun-"))
        self._orig = mm.PRESETS.copy()
        mm.PRESETS["fast"] = replace(mm.PRESETS["fast"], min_bytes=100, sha256=None)

    def tearDown(self):
        mm.PRESETS.clear(); mm.PRESETS.update(self._orig)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gen(self, **kw):
        return CommandEditGenerator("fast", model_root=self.tmp, worker_command=_WORKER,
                                    env={ENV_FAKE_BACKEND: "1"}, generate_timeout=20, **kw)

    def test_missing_model_raises(self):
        with self.assertRaises(CommandEditModelMissing):
            self._gen().run("текст", "зроби офіційніше")

    def test_empty_inputs_return_empty(self):
        _make_ready_model(self.tmp)
        self.assertEqual(self._gen().run("   ", "команда"), "")
        self.assertEqual(self._gen().run("текст", "  "), "")

    def test_fake_backend_rejected_not_stub(self):
        """Блокер (урок рецензента): без llama worker бере FakeBackend (заглушка).
        run() має підняти CommandEditError «недоступна», а НЕ повернути заглушку
        за успіх — інакше виділення заміниться сміттям. Сайдкар прибрано у finally."""
        _make_ready_model(self.tmp)
        g = self._gen()
        with self.assertRaises(CommandEditError) as ctx:
            g.run("надиктований фрагмент", "зроби офіційніше")
        self.assertIn("недоступна", str(ctx.exception))
        self.assertIsNone(g._sidecar)

    def test_cancel_before_run_raises_cancelled(self):
        _make_ready_model(self.tmp)
        g = self._gen()
        g.cancel()
        with self.assertRaises(CommandEditCancelled):
            g.run("текст", "зроби офіційніше")


# ─────────────────────────── 3. зчитування виділеного ───────────────────────
class CaptureSelectionTests(unittest.TestCase):
    """paste.capture_selection із інжектованими залежностями (без реального буфера
    й клавіатури). Головне: буфер користувача ЗБЕРІГАЄТЬСЯ й ВІДНОВЛЮЄТЬСЯ."""

    def _fake_clipboard(self, *, initial, selection):
        """Модель буфера: paste_fn читає стан; send_copy імітує Ctrl+C, кладучи
        виділене (None = нічого не виділено, буфер лишається як був)."""
        from fronts.desktop import paste
        state = {"value": initial}
        log = []

        def copy_fn(text):
            state["value"] = text
            log.append(("copy", text))

        def paste_fn():
            return state["value"]

        def send_copy():
            log.append(("ctrl_c", None))
            if selection is not None:
                state["value"] = selection      # активне вікно поклало виділене
            return True

        return paste, state, log, copy_fn, paste_fn, send_copy

    def test_returns_selection_and_restores_clipboard(self):
        paste, state, log, copy_fn, paste_fn, send_copy = self._fake_clipboard(
            initial="важливий текст користувача", selection="виділений фрагмент")
        out = paste.capture_selection(
            copy_fn=copy_fn, paste_fn=paste_fn, send_copy=send_copy,
            sleep_fn=lambda _s: None)
        self.assertEqual(out, "виділений фрагмент")
        # буфер користувача повернуто на місце
        self.assertEqual(state["value"], "важливий текст користувача")

    def test_no_selection_returns_empty_and_restores(self):
        # Ctrl+C нічого не копіює (selection=None) → буфер лишається сентинелом →
        # повертаємо "" і відновлюємо початковий текст.
        paste, state, log, copy_fn, paste_fn, send_copy = self._fake_clipboard(
            initial="буфер користувача", selection=None)
        out = paste.capture_selection(
            copy_fn=copy_fn, paste_fn=paste_fn, send_copy=send_copy,
            sleep_fn=lambda _s: None)
        self.assertEqual(out, "")
        self.assertEqual(state["value"], "буфер користувача")

    def test_sentinel_never_leaks_into_result(self):
        paste, state, log, copy_fn, paste_fn, send_copy = self._fake_clipboard(
            initial="x", selection=None)
        out = paste.capture_selection(
            copy_fn=copy_fn, paste_fn=paste_fn, send_copy=send_copy,
            sleep_fn=lambda _s: None)
        self.assertNotIn("balachky", out)
        self.assertEqual(out, "")

    def test_empty_previous_clipboard_not_restored(self):
        # previous порожній (буфер був порожній / не-текст) → відновлювати нічого,
        # але виділене все одно повертаємо.
        paste, state, log, copy_fn, paste_fn, send_copy = self._fake_clipboard(
            initial="", selection="виділене")
        out = paste.capture_selection(
            copy_fn=copy_fn, paste_fn=paste_fn, send_copy=send_copy,
            sleep_fn=lambda _s: None)
        self.assertEqual(out, "виділене")
        # порожній previous не відновлюємо → жодного copy(previous="")
        self.assertNotIn(("copy", ""), log)


if __name__ == "__main__":
    unittest.main()
