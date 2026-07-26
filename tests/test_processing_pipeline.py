"""feature/processing-slider — гейт рівнів обробки у PTT-конвеєрі (_work) (спека §7, §9).

Ключова гарантія (скарга користувачів №1): повзунок міняє ЛИШЕ текст-ВИВІД, а
історія завжди зберігає ДОСЛІВНИЙ сирий транскрипт. Стиль харнеса — як
NavPipelineGateTests у test_nav_pipeline.py (розв'язаний _work на фейк-контролері).
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fronts.desktop import app as desktop_app
from whisper_core.processing import policy_for_mode, ProcessingMode
from whisper_core.fillers import apply_filler_cleanup


class _Signal:
    def __init__(self):
        self.count = 0
        self.last = None

    def emit(self, *args):
        self.count += 1
        self.last = args


class _Transcribed:
    def __init__(self):
        self.calls = []

    def emit(self, raw, fin, words, ts):
        self.calls.append((raw, fin))


def _controller(raw, glossary, *, macros=None, voice_punctuation=False):
    enhance_calls = {"n": 0}

    def _enhance(final, terms, policy=None):
        enhance_calls["n"] += 1
        return final

    controller = SimpleNamespace(
        recorder=SimpleNamespace(to_audio=lambda chunks: "audio"),
        _transcribe_with_fallback=lambda audio, terms, **_kw: (raw, glossary, 1.0, [], []),
        output_mode="show",                       # без вставки — лише картка/історія
        transcription_error=_Signal(),
        transcribed=_Transcribed(),
        formfill_text=_Signal(),
        preview_ready=_Signal(),
        finished=_Signal(),
        macros=(macros or {}),
        formfill_capturing=False,
        _apply_text_enhancements=_enhance,
        cfg=SimpleNamespace(sounds=False, voice_punctuation=voice_punctuation,
                            voice_nav_enabled=False, language="uk"),
        _busy=True,
    )
    return controller, enhance_calls


def _run(controller, policy):
    profile = SimpleNamespace(history_path="hp", memory_enabled=False)
    with patch.object(desktop_app, "log_history", return_value=None) as lh:
        desktop_app.DesktopApp._work(controller, ["chunk"], profile, object(),
                                     policy=policy)
    return lh


class VerbatimMode(unittest.TestCase):
    def test_output_equals_raw_and_bypasses_everything(self):
        # словник змінив би регістр, макрос замінив би фразу, голосова пунктуація
        # спрацювала б — у «Дослівно» НІЧОГО з цього; вивід дорівнює сирому.
        ctrl, enh = _controller(
            "привіт світ крапка", "ПРИВІТ СВІТ.",
            macros={"привіт світ крапка": "МАКРОС"}, voice_punctuation=True)
        lh = _run(ctrl, policy_for_mode(ProcessingMode.VERBATIM))
        self.assertEqual(ctrl.transcribed.calls[-1][1], "привіт світ крапка")
        self.assertEqual(enh["n"], 0)                         # покращення не викликані
        # історія: raw незайманий, final == сирий (нічого не змінили)
        self.assertEqual(lh.call_args.args[1], "привіт світ крапка")
        self.assertEqual(lh.call_args.args[2], "привіт світ крапка")


class FillersMode(unittest.TestCase):
    def test_medium_cleanup_only_bypassing_glossary(self):
        raw = "я я хотів ееее сказати"
        ctrl, enh = _controller(raw, "Я Я ХОТІВ сказати ЗІ СЛОВНИКА")
        lh = _run(ctrl, policy_for_mode(ProcessingMode.FILLERS))
        expected = apply_filler_cleanup(raw, "medium")        # "я хотів сказати"
        self.assertEqual(ctrl.transcribed.calls[-1][1], expected)
        self.assertNotIn("СЛОВНИКА", ctrl.transcribed.calls[-1][1])  # словник обійдено
        self.assertEqual(enh["n"], 0)
        self.assertEqual(lh.call_args.args[1], raw)           # історія: дослівний raw
        self.assertEqual(lh.call_args.args[2], expected)


class DocumentMode(unittest.TestCase):
    def test_uses_glossary_and_allows_macros_but_keeps_raw(self):
        ctrl, _ = _controller("привіт", "вітаю", macros={"вітаю": "ВІТАННЯ"})
        lh = _run(ctrl, policy_for_mode(ProcessingMode.DOCUMENT))
        # стартує від словникового «вітаю», макрос дозволено → «ВІТАННЯ»
        self.assertEqual(ctrl.transcribed.calls[-1][1], "ВІТАННЯ")
        # але історія все одно береже ДОСЛІВНИЙ сирий транскрипт
        self.assertEqual(lh.call_args.args[1], "привіт")

    def test_allows_enhancements_when_no_macro(self):
        ctrl, enh = _controller("привіт", "вітаю")            # без макроса
        _run(ctrl, policy_for_mode(ProcessingMode.DOCUMENT))
        self.assertEqual(enh["n"], 1)                         # покращення дозволені


class LegacyNoneKeepsOldBehavior(unittest.TestCase):
    def test_policy_none_uses_glossary(self):
        # старий шлях (наявні тести/моки): policy=None → словниковий final, як досі
        ctrl, _ = _controller("сирий", "СЛОВНИКОВИЙ")
        lh = _run(ctrl, None)
        self.assertEqual(ctrl.transcribed.calls[-1][1], "СЛОВНИКОВИЙ")
        self.assertEqual(lh.call_args.args[1], "сирий")       # raw усе одно незайманий


if __name__ == "__main__":
    unittest.main()
