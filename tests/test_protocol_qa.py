"""Q&A по нараді: чисті функції промту/парсингу цитат + оркестрація з інжектованим
фейковим LLM (без моделі), і повний прогін через справжній worker-підпроцес із
фейк-бекендом (гейт «тихої заглушки»)."""
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.protocol import ENV_FAKE_BACKEND
from whisper_core.protocol import model_manager as mm
from whisper_core.protocol import qa
from whisper_core.protocol.service import (QAGenerator, QACancelled, QAError,
                                           QAModelMissing)
from whisper_core.meeting.postprocess import Utterance

_WORKER = [sys.executable, "-m", "whisper_core.protocol.worker"]

_CANNED_ANSWER = ("До п'ятниці треба підготувати звіт по пальному "
                  "[00:00]. Цифри бере перша рота [00:12].")


def _fake_generate(prompt, *, max_tokens=1024, n_ctx=None):
    return _CANNED_ANSWER


def _make_ready_model(root: Path):
    d = root / "fast"
    d.mkdir(parents=True)
    (d / mm.MODEL_FILENAME).write_bytes(b"GGUF" + b"\x00" * 500)
    (d / mm._READY_MARKER).write_text("ok", encoding="utf-8")
    return d


class TestPromptBuilding(unittest.TestCase):
    def test_prompt_contains_question_transcript_and_grounding_rule(self):
        p = qa.build_qa_prompt("Хто готує звіт?", "[00:00] Я: тест")
        self.assertIn("Хто готує звіт?", p)
        self.assertIn("[00:00] Я: тест", p)
        # головне правило анти-галюцинації присутнє
        self.assertIn("ЛИШЕ", p)
        self.assertIn("немає відповіді", p.lower())

    def test_synthesis_prompt_keeps_question_and_parts(self):
        p = qa._synthesis_prompt("Питання?", ["відповідь A [00:05]", "відповідь B"])
        self.assertIn("Питання?", p)
        self.assertIn("Частина 1", p)
        self.assertIn("Частина 2", p)
        self.assertIn("[00:05]", p)


class TestCitationParsing(unittest.TestCase):
    def test_parses_timecodes_in_order_without_dupes(self):
        cites = qa.parse_citations("Ось тут [01:05] і ще раз [01:05], а потім [00:12].")
        self.assertEqual(cites, [(65, "01:05"), (12, "00:12")])

    def test_parses_hms_timecode(self):
        self.assertEqual(qa.parse_citations("огляд [1:02:03]"), [(3723, "1:02:03")])

    def test_no_citations_returns_empty(self):
        self.assertEqual(qa.parse_citations("немає таймкодів"), [])
        self.assertEqual(qa.parse_citations(""), [])


class TestAnswerQuestion(unittest.TestCase):
    def _us(self):
        return [Utterance(0.0, 3.0, "me", "готуємо звіт по пальному", source="mic"),
                Utterance(12.0, 15.0, "others", "візьму цифри з першої роти", source="sys")]

    def test_empty_question_returns_empty(self):
        self.assertEqual(qa.answer_question("", self._us(), _fake_generate), "")

    def test_empty_utterances_returns_empty(self):
        self.assertEqual(qa.answer_question("Хто?", [], _fake_generate), "")

    def test_single_pass_returns_answer_with_citations(self):
        out = qa.answer_question("Хто готує звіт?", self._us(), _fake_generate)
        self.assertIn("звіт", out)
        self.assertTrue(qa.parse_citations(out))          # цитати збереглись

    def test_single_pass_calls_generate_once(self):
        calls = []

        def counting(prompt, *, max_tokens=1024, n_ctx=None):
            calls.append(prompt)
            return _CANNED_ANSWER
        qa.answer_question("Хто?", self._us(), counting)
        self.assertEqual(len(calls), 1)

    def test_long_meeting_triggers_chunking_and_synthesis(self):
        big = "слово " * 2000
        us = [Utterance(float(i * 10), float(i * 10 + 9), "me", big, source="mic")
              for i in range(60)]
        transcript = qa._gen.render_transcript(
            us, me_label="Я", others_label="Співрозмовники")
        self.assertEqual(qa._gen.plan_context(
            qa._gen.estimate_tokens(transcript)).mode, "mapreduce")
        calls = []

        def counting(prompt, *, max_tokens=1024, n_ctx=None):
            calls.append(prompt)
            return _CANNED_ANSWER
        out = qa.answer_question("Про що нарада?", us, counting)
        self.assertTrue(out)
        self.assertGreater(len(calls), 1)                 # кілька чанків
        self.assertIn("Зведена відповідь", calls[-1])     # останній — синтез


class TestQAGeneratorRun(unittest.TestCase):
    """Оркестрація QAGenerator через справжній worker-підпроцес із фейк-бекендом."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qarun-"))
        self._orig = mm.PRESETS.copy()
        mm.PRESETS["fast"] = replace(mm.PRESETS["fast"], min_bytes=100, sha256=None)
        self.us = [Utterance(0.0, 3.0, "me", "привіт", source="mic")]

    def tearDown(self):
        mm.PRESETS.clear(); mm.PRESETS.update(self._orig)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gen(self, **kw):
        return QAGenerator("fast", model_root=self.tmp, worker_command=_WORKER,
                           env={ENV_FAKE_BACKEND: "1"}, generate_timeout=20, **kw)

    def test_missing_model_raises(self):
        with self.assertRaises(QAModelMissing):
            self._gen().run("Хто?", self.us)

    def test_available_reflects_model(self):
        g = self._gen()
        self.assertFalse(g.available())
        _make_ready_model(self.tmp)
        self.assertTrue(g.available())

    def test_fake_backend_rejected_not_shown_as_answer(self):
        """Блокер (урок судді): без llama worker бере FakeBackend, чий вихід —
        позначка. run() має відхилити її як QAError з UA-повідомленням «встановіть
        компонент», а НЕ повернути заглушку за відповідь."""
        _make_ready_model(self.tmp)
        g = self._gen()
        with self.assertRaises(QAError) as ctx:
            g.run("Хто готує звіт?", self.us)
        self.assertIn("недоступна", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, QACancelled)
        self.assertIsNone(g._sidecar)                     # сайдкар зупинено у finally

    def test_empty_question_returns_empty(self):
        _make_ready_model(self.tmp)
        self.assertEqual(self._gen().run("", self.us), "")

    def test_cancel_before_run_raises_cancelled(self):
        _make_ready_model(self.tmp)
        g = self._gen()
        g.cancel()
        with self.assertRaises(QACancelled):
            g.run("Хто?", self.us)

    def test_sidecar_crash_becomes_qa_error(self):
        _make_ready_model(self.tmp)
        crashing = [sys.executable, "-c", "raise SystemExit(1)"]
        g = QAGenerator("fast", model_root=self.tmp, worker_command=crashing,
                        generate_timeout=10)
        with self.assertRaises(QAError) as ctx:
            g.run("Хто?", self.us)
        self.assertNotIsInstance(ctx.exception, QACancelled)
        self.assertTrue(str(ctx.exception))
        self.assertIsNone(g._sidecar)


if __name__ == "__main__":
    unittest.main()
