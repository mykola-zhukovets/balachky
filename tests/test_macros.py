"""feature/voice-macros — тести чистої логіки голосових макросів
(whisper_core.macros) та гейта інтеграції в PTT-конвеєрі (_work).
Стиль — як test_snippets.py."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from whisper_core.macros import (
    expand_placeholders, load_macros, apply_macro,
    save_macros, add_macro, delete_macro, migrate_snippets,
)


class ExpandPlaceholderTests(unittest.TestCase):
    NOW = datetime(2026, 7, 17, 14, 5)

    def test_date_uk(self):
        self.assertEqual(expand_placeholders("{дата}", self.NOW), "17.07.2026")

    def test_time_uk(self):
        self.assertEqual(expand_placeholders("{час}", self.NOW), "14:05")

    def test_date_en_alias(self):
        self.assertEqual(expand_placeholders("{date}", self.NOW), "17.07.2026")

    def test_time_en_alias(self):
        self.assertEqual(expand_placeholders("{time}", self.NOW), "14:05")

    def test_token_case_insensitive(self):
        self.assertEqual(expand_placeholders("{Дата} {ЧАС}", self.NOW),
                         "17.07.2026 14:05")

    def test_multiple_and_surrounding_text(self):
        self.assertEqual(
            expand_placeholders("Складено {дата} о {час}.", self.NOW),
            "Складено 17.07.2026 о 14:05.")

    def test_unknown_token_kept(self):
        self.assertEqual(expand_placeholders("{інше} {дата}", self.NOW),
                         "{інше} 17.07.2026")

    def test_no_placeholder_unchanged(self):
        self.assertEqual(expand_placeholders("звичайний текст", self.NOW),
                         "звичайний текст")

    def test_empty_unchanged(self):
        self.assertEqual(expand_placeholders("", self.NOW), "")

    def test_default_now_is_used(self):
        # без явного now підстановка все одно відбувається (не лишає {дата})
        self.assertNotIn("{дата}", expand_placeholders("{дата}"))


class LoadTests(unittest.TestCase):
    def _write(self, text):
        p = Path(tempfile.mkdtemp()) / "macros.toml"
        p.write_text(text, encoding="utf-8")
        return p

    def test_missing_file_is_empty(self):
        self.assertEqual(load_macros(Path(tempfile.mkdtemp()) / "nope.toml"), {})

    def test_keys_are_normalized(self):
        p = self._write('"Мій Підпис" = "текст"\n')
        self.assertEqual(load_macros(p), {"мій підпис": "текст"})

    def test_broken_toml_is_empty_dict(self):
        p = self._write('"тригер = незакрито\n')
        self.assertEqual(load_macros(p), {})

    def test_multiline_value(self):
        p = self._write('"шапка" = """\nрядок один\nрядок два"""\n')
        self.assertEqual(load_macros(p), {"шапка": "рядок один\nрядок два"})


class ApplyExactMatchTests(unittest.TestCase):
    NOW = datetime(2026, 7, 17, 14, 5)
    MACROS = {"мій підпис": "З повагою, Микола\nСкладено {дата}"}

    def test_exact_match_expands_with_date(self):
        self.assertEqual(
            apply_macro("мій підпис", self.MACROS, self.NOW),
            "З повагою, Микола\nСкладено 17.07.2026")

    def test_match_is_soft_normalized(self):
        # регістр, зайві пробіли й кінцева крапка не заважають збігу
        self.assertEqual(
            apply_macro("Мій  Підпис.", self.MACROS, self.NOW),
            "З повагою, Микола\nСкладено 17.07.2026")

    def test_partial_match_not_replaced(self):
        # тригер лише ЧАСТИНА фрази — не замінюємо (безпека, п.4 ТЗ)
        self.assertEqual(apply_macro("мій підпис будь ласка", self.MACROS, self.NOW),
                         "мій підпис будь ласка")

    def test_embedded_match_not_replaced(self):
        self.assertEqual(apply_macro("а тепер мій підпис тут", self.MACROS, self.NOW),
                         "а тепер мій підпис тут")

    def test_no_match_unchanged(self):
        self.assertEqual(apply_macro("зовсім інше", self.MACROS, self.NOW),
                         "зовсім інше")

    def test_empty_text_unchanged(self):
        self.assertEqual(apply_macro("", self.MACROS, self.NOW), "")

    def test_empty_macros_unchanged(self):
        self.assertEqual(apply_macro("мій підпис", {}, self.NOW), "мій підпис")

    def test_macro_without_placeholders_returned_whole(self):
        m = {"адреса": "вул. Соборна 5, Одеса"}
        self.assertEqual(apply_macro("адреса", m, self.NOW), "вул. Соборна 5, Одеса")


class WriteRoundTripTests(unittest.TestCase):
    def _path(self):
        return Path(tempfile.mkdtemp()) / "macros.toml"

    def test_save_load_multiline_with_placeholder(self):
        p = self._path()
        text = "Протокол від {дата}\nЧас: {час}\nПідпис"
        save_macros(p, {"протокол": text})
        self.assertEqual(load_macros(p), {"протокол": text})

    def test_add_normalizes_trigger(self):
        p = self._path()
        add_macro(p, "Мій Підпис", "текст {дата}")
        self.assertEqual(load_macros(p), {"мій підпис": "текст {дата}"})

    def test_add_empty_trigger_is_noop(self):
        p = self._path()
        add_macro(p, "   ", "текст")
        self.assertEqual(load_macros(p), {})

    def test_delete_removes(self):
        p = self._path()
        add_macro(p, "один", "1")
        add_macro(p, "два", "2")
        self.assertTrue(delete_macro(p, "Один."))   # м'яка звірка тригера
        self.assertEqual(load_macros(p), {"два": "2"})

    def test_delete_missing_returns_false(self):
        p = self._path()
        add_macro(p, "один", "1")
        self.assertFalse(delete_macro(p, "нема"))


class MigrateSnippetsTests(unittest.TestCase):
    """Одноразова міграція злиття: глобальні сніпети → macros.toml профілю."""

    def _paths(self):
        d = Path(tempfile.mkdtemp())
        return d / "snippets.toml", d / "macros.toml"

    def test_no_snippets_file_is_noop(self):
        sp, mp = self._paths()
        self.assertEqual(migrate_snippets(sp, mp), 0)
        self.assertFalse(mp.exists())

    def test_snippets_moved_into_macros(self):
        sp, mp = self._paths()
        save_macros(sp, {"встав підпис": "З повагою, Микола"})   # той самий TOML-формат
        self.assertEqual(migrate_snippets(sp, mp), 1)
        self.assertEqual(load_macros(mp), {"встав підпис": "З повагою, Микола"})

    def test_snippets_file_removed_after_migration(self):
        # відсутність файлу — маркер «вже мігровано»: повторний виклик = no-op
        sp, mp = self._paths()
        save_macros(sp, {"адреса": "вул. Соборна 5"})
        migrate_snippets(sp, mp)
        self.assertFalse(sp.exists())
        self.assertEqual(migrate_snippets(sp, mp), 0)

    def test_existing_macro_wins_on_collision(self):
        # макрос із тим самим тригером НЕ перезаписується сніпетом
        sp, mp = self._paths()
        save_macros(sp, {"підпис": "старий сніпет"})
        save_macros(mp, {"підпис": "новий макрос"})
        self.assertEqual(migrate_snippets(sp, mp), 0)
        self.assertEqual(load_macros(mp), {"підпис": "новий макрос"})

    def test_merge_keeps_both_distinct(self):
        sp, mp = self._paths()
        save_macros(sp, {"сніпет": "S"})
        save_macros(mp, {"макрос": "M"})
        self.assertEqual(migrate_snippets(sp, mp), 1)
        self.assertEqual(load_macros(mp), {"макрос": "M", "сніпет": "S"})

    def test_empty_snippets_file_removed_without_writing_macros(self):
        sp, mp = self._paths()
        sp.write_text("", encoding="utf-8")
        self.assertEqual(migrate_snippets(sp, mp), 0)
        self.assertFalse(sp.exists())
        self.assertFalse(mp.exists())   # порожня міграція не створює macros.toml


class PipelineGateTests(unittest.TestCase):
    """Гейт у _work: макрос застосовується (з підстановкою дати/часу) лише при
    точному збігу всієї фрази й, як сніпет, пропускає голосову пунктуацію."""

    @staticmethod
    def _controller(macros, voice_punctuation=False, final="мій підпис"):
        recorded = {}

        class Transcribed:
            def emit(self, raw, fin, words, ts):
                recorded["final"] = fin

        class Counter:
            def __init__(self):
                self.count = 0

            def emit(self, *args):
                self.count += 1

        controller = SimpleNamespace(
            recorder=SimpleNamespace(to_audio=lambda chunks: "audio"),
            _transcribe_with_fallback=lambda audio, terms, **_kw: ("raw", final, 1.0, [], []),
            output_mode="show",
            transcription_error=Counter(),
            transcribed=Transcribed(),
            finished=Counter(),
            macros=macros,
            cfg=SimpleNamespace(sounds=False, voice_punctuation=voice_punctuation,
                                language="uk"),
            _busy=True,
        )
        return controller, recorded

    def _run(self, controller):
        from fronts.desktop import app as desktop_app
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "log_history", return_value=None):
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())

    def test_macro_expands_exact_phrase(self):
        controller, recorded = self._controller({"мій підпис": "З повагою, Микола"})
        self._run(controller)
        self.assertEqual(recorded["final"], "З повагою, Микола")

    def test_macro_substitutes_date(self):
        controller, recorded = self._controller({"дата зараз": "Сьогодні {дата}"},
                                                final="дата зараз")
        self._run(controller)
        self.assertTrue(recorded["final"].startswith("Сьогодні "))
        self.assertNotIn("{дата}", recorded["final"])

    def test_macro_not_triggered_mid_phrase(self):
        # п.4 ТЗ: макрос НЕ спрацьовує посеред довшої фрази — лише повний збіг
        controller, recorded = self._controller(
            {"мій підпис": "З повагою, Микола"},
            final="постав мій підпис будь ласка")
        self._run(controller)
        self.assertEqual(recorded["final"], "постав мій підпис будь ласка")

    def test_macro_skips_voice_punctuation(self):
        # текст макросу дослівний — слово-команда «Кома» в ньому не стає символом
        controller, recorded = self._controller(
            {"адреса": "Кома Наталія 5"}, voice_punctuation=True, final="адреса")
        self._run(controller)
        self.assertEqual(recorded["final"], "Кома Наталія 5")

    def test_no_macro_leaves_text(self):
        controller, recorded = self._controller({"інший": "щось"},
                                                final="звичайний текст")
        self._run(controller)
        self.assertEqual(recorded["final"], "звичайний текст")


if __name__ == "__main__":
    unittest.main()
