"""E3: генерація протоколу — чисті функції промту/постобробки + оркестрація з
інжектованим фейковим LLM (без моделі)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.protocol import generate as gen
from whisper_core.meeting.postprocess import Utterance

_CANNED = """## Підсумок
Тест.

## Рішення
- Рішення один.

## Задачі
| Хто | Що | Термін | Час у записі |
|-----|-----|--------|--------------|
| Я | Зробити | завтра | 00:00 |

## Розділи наради
- [00:00–00:10] Тема"""


def _fake_generate(prompt, *, max_tokens=2048, n_ctx=None):
    return _CANNED


class TestPureHelpers(unittest.TestCase):
    def test_estimate_tokens(self):
        self.assertEqual(gen.estimate_tokens("a" * 100), 35)

    def test_render_transcript_has_labels_and_timecodes(self):
        us = [Utterance(0.0, 3.0, "me", "привіт", source="mic"),
              Utterance(3.0, 6.0, "others", "вітаю", source="sys")]
        text = gen.render_transcript(us, me_label="Я", others_label="Співрозмовники")
        self.assertIn("[00:00] Я: привіт", text)
        self.assertIn("[00:03] Співрозмовники: вітаю", text)

    def test_build_prompt_contains_transcript_and_fewshot(self):
        p = gen.build_prompt("[00:00] Я: тест")
        self.assertIn("[00:00] Я: тест", p)
        self.assertIn("Приклад 1", p)
        self.assertIn("## Задачі", p)
        self.assertIn("секретар", p.lower())


class TestPostprocess(unittest.TestCase):
    def test_strips_think_tags(self):
        out = gen.postprocess("<think>міркування</think>\n## Підсумок\nОк")
        self.assertNotIn("think", out.lower())
        self.assertTrue(out.startswith("## Підсумок"))

    def test_strips_code_fence(self):
        out = gen.postprocess("```markdown\n## Підсумок\nОк\n```")
        self.assertEqual(out, "## Підсумок\nОк")

    def test_empty_input(self):
        self.assertEqual(gen.postprocess(""), "")

    def test_is_valid_protocol(self):
        self.assertTrue(gen.is_valid_protocol(_CANNED))
        self.assertFalse(gen.is_valid_protocol("просто текст"))


class TestGenerateProtocol(unittest.TestCase):
    def test_empty_utterances_returns_empty(self):
        self.assertEqual(gen.generate_protocol([], _fake_generate), "")

    def test_single_pass(self):
        us = [Utterance(0.0, 3.0, "me", "привіт", source="mic")]
        out = gen.generate_protocol(us, _fake_generate)
        self.assertTrue(gen.is_valid_protocol(out))
        self.assertIn("## Задачі", out)

    def test_single_pass_calls_generate_once(self):
        us = [Utterance(0.0, 3.0, "me", "привіт", source="mic")]
        calls = []

        def counting(prompt, *, max_tokens=2048, n_ctx=None):
            calls.append(prompt)
            return _CANNED
        gen.generate_protocol(us, counting)
        self.assertEqual(len(calls), 1)

    def test_long_meeting_triggers_chunking_and_synthesis(self):
        # Багато довгих реплік → перевищення стелі контексту → map (кілька) + synthesis.
        big = "слово " * 2000
        us = [Utterance(float(i * 10), float(i * 10 + 9), "me", big, source="mic")
              for i in range(60)]
        # переконуємось, що план справді йде в map-reduce
        transcript = gen.render_transcript(us, me_label="Я", others_label="Співрозмовники")
        plan = gen.plan_context(gen.estimate_tokens(transcript))
        self.assertEqual(plan.mode, "mapreduce")

        calls = []

        def counting(prompt, *, max_tokens=2048, n_ctx=None):
            calls.append((prompt, n_ctx))
            return _CANNED
        out = gen.generate_protocol(us, counting)
        self.assertTrue(gen.is_valid_protocol(out))
        self.assertGreater(len(calls), 1)                 # кілька чанків
        self.assertIn("Зведений протокол", calls[-1][0])  # останній — синтез
        # n_ctx СТАБІЛЬНИЙ на весь прогін (модель не перевантажується між чанками)
        self.assertEqual(len({n for _p, n in calls}), 1)


class TestPlanContext(unittest.TestCase):
    def test_small_transcript_single_pass_smallest_step(self):
        plan = gen.plan_context(200, model_ctx_cap=32768, overhead=800, max_tokens=2048)
        self.assertEqual(plan.mode, "single")
        self.assertEqual(plan.n_ctx, 8192)          # найменша сходинка вистачає

    def test_grows_ladder_step_when_needed(self):
        # бюджет ~ 800+7000+2048 = 9848 > 8192 → наступна сходинка 16384, single
        plan = gen.plan_context(7000, model_ctx_cap=32768, overhead=800, max_tokens=2048)
        self.assertEqual(plan.mode, "single")
        self.assertEqual(plan.n_ctx, 16384)

    def test_regression_root_bug_6000_tokens_at_cap_8192_goes_mapreduce(self):
        # Точний вхід, що ЛАМАВ прод: ~6000 ток. транскрипту при стелі 8192 раніше
        # ішов у single-pass (n_ctx=8192) і llama кидала overflow ДО чанкування.
        # Тепер бюджет (overhead+6000+2048) > 8192 → mapreduce на стелі.
        plan = gen.plan_context(6000, model_ctx_cap=8192, overhead=850, max_tokens=2048)
        self.assertEqual(plan.mode, "mapreduce")
        self.assertEqual(plan.n_ctx, 8192)
        # чанк влазить у стелю з запасом
        self.assertLessEqual(850 + plan.chunk_tokens + 2048, 8192)

    def test_threshold_is_derived_from_ctx_not_hardcoded_90k(self):
        # 20000 токенів при стелі 32768 ще влазить одним проходом (не «90К»)
        plan = gen.plan_context(20000, model_ctx_cap=32768, overhead=850, max_tokens=2048)
        self.assertEqual(plan.mode, "single")
        self.assertEqual(plan.n_ctx, 32768)

    def test_overflow_when_cap_too_small_for_any_chunk(self):
        with self.assertRaises(gen.ProtocolContextOverflow):
            gen.plan_context(100000, model_ctx_cap=2048, overhead=850, max_tokens=2048)

    def test_giant_single_utterance_raises_overflow(self):
        big = "слово " * 4000                       # одна репліка більша за будь-який чанк
        us = [Utterance(0.0, 9.0, "me", big, source="mic") for _ in range(40)]
        with self.assertRaises(gen.ProtocolContextOverflow):
            gen.generate_protocol(us, _fake_generate, model_ctx_cap=8192)


class TestChunking(unittest.TestCase):
    def test_chunks_respect_token_budget_with_overlap(self):
        us = [Utterance(float(i), float(i + 1), "me", "x" * 100, source="mic")
              for i in range(20)]
        chunks = gen._chunk_utterances(us, chunk_tokens=100, overlap_tokens=20)
        self.assertGreater(len(chunks), 1)
        # кожна репліка присутня хоча б в одному чанку
        seen = {id(u) for c in chunks for u in c}
        self.assertEqual(len(seen), 20)


if __name__ == "__main__":
    unittest.main()
