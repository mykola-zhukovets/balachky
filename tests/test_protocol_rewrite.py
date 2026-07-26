"""feature/output-formats — AI-переформатування надиктованого тексту.

Ядро (rewrite.py) без моделі: формування промту зі шаблону / власного промту,
постобробка, порожній вхід. Плюс оркестрація RewriteGenerator через СПРАВЖНІЙ
worker-підпроцес із фейк-бекендом: гейт «встановіть компонент» замість заглушки,
гейт відсутньої моделі, скасування.
"""
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.protocol import ENV_FAKE_BACKEND
from whisper_core.protocol import model_manager as mm
from whisper_core.protocol import rewrite as rw
from whisper_core.protocol.service import (RewriteGenerator, RewriteError,
                                           RewriteCancelled, RewriteModelMissing)

_WORKER = [sys.executable, "-m", "whisper_core.protocol.worker"]


# ─────────────────────────── ядро: промти ───────────────────────────
class PromptTests(unittest.TestCase):
    def test_all_templates_build_prompt_with_text(self):
        for tid in rw.TEMPLATE_IDS:
            prompt = rw.build_rewrite_prompt(tid, "купити хліб зателефонувати")
            self.assertIn("Надиктований текст", prompt)
            self.assertIn("купити хліб зателефонувати", prompt)
            self.assertIn(rw.TEMPLATES[tid].split("\n")[0].split(".")[0][:10], prompt)

    def test_unknown_template_raises(self):
        with self.assertRaises(rw.UnknownTemplate):
            rw.build_rewrite_prompt("nonsense", "текст")

    def test_custom_prompt_overrides_template(self):
        p = rw.build_rewrite_prompt("letter", "текст", custom_prompt="ЗРОБИ ТАБЛИЦЮ")
        self.assertIn("ЗРОБИ ТАБЛИЦЮ", p)

    def test_custom_prompt_allows_unknown_template_id(self):
        # власний промт → id можна не знати
        p = rw.build_rewrite_prompt("", "текст", custom_prompt="ПЕРЕПИШИ")
        self.assertIn("ПЕРЕПИШИ", p)

    def test_empty_custom_prompt_falls_back_to_template(self):
        p = rw.build_rewrite_prompt("concise", "текст", custom_prompt="   ")
        self.assertEqual(rw.system_prompt("concise"), rw.TEMPLATES["concise"])
        self.assertIn("стисн", p.lower())


class RewriteTextTests(unittest.TestCase):
    def test_empty_text_returns_empty_without_calling_model(self):
        called = []
        out = rw.rewrite_text("  ", "letter", lambda p, **k: called.append(p) or "x")
        self.assertEqual(out, "")
        self.assertEqual(called, [])

    def test_postprocesses_model_output(self):
        # generate_fn повертає з code-fence — постобробка знімає обгортку
        out = rw.rewrite_text("текст", "concise",
                              lambda p, **k: "```\nкоротко\n```")
        self.assertEqual(out, "коротко")

    def test_passes_prompt_to_generate_fn(self):
        seen = {}
        rw.rewrite_text("моя чернетка", "tasklist",
                        lambda p, **k: seen.update(prompt=p) or "- ок")
        self.assertIn("моя чернетка", seen["prompt"])


# ─────────────────────────── оркестрація ───────────────────────────
def _make_ready_model(root: Path):
    d = root / "fast"
    d.mkdir(parents=True)
    (d / mm.MODEL_FILENAME).write_bytes(b"GGUF" + b"\x00" * 500)
    (d / mm._READY_MARKER).write_text("ok", encoding="utf-8")
    return d


class RewriteGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rwrun-"))
        self._orig = mm.PRESETS.copy()
        mm.PRESETS["fast"] = replace(mm.PRESETS["fast"], min_bytes=100, sha256=None)

    def tearDown(self):
        mm.PRESETS.clear(); mm.PRESETS.update(self._orig)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gen(self, **kw):
        return RewriteGenerator("fast", model_root=self.tmp, worker_command=_WORKER,
                                env={ENV_FAKE_BACKEND: "1"}, generate_timeout=20, **kw)

    def test_missing_model_raises(self):
        with self.assertRaises(RewriteModelMissing):
            self._gen().run("текст", "letter")

    def test_empty_text_returns_empty(self):
        _make_ready_model(self.tmp)
        self.assertEqual(self._gen().run("   ", "letter"), "")

    def test_fake_backend_rejected_not_stub(self):
        """Блокер (урок судді): без llama worker бере FakeBackend (заглушка).
        run() має підняти RewriteError «встановіть компонент», а НЕ повернути
        заглушку за успіх. Сайдкар прибрано у finally."""
        _make_ready_model(self.tmp)
        g = self._gen()
        with self.assertRaises(RewriteError) as ctx:
            g.run("надиктована чернетка", "letter")
        self.assertIn("недоступна", str(ctx.exception))
        self.assertIsNone(g._sidecar)

    def test_cancel_before_run_raises_cancelled(self):
        _make_ready_model(self.tmp)
        g = self._gen()
        g.cancel()
        with self.assertRaises(RewriteCancelled):
            g.run("текст", "letter")


if __name__ == "__main__":
    unittest.main()
