"""Дешевий статичний AST-лінт проти тавтологічних i18n-тестів (Патерн 2 з
аудиту тавтологічних тестів 30.07.2026).

Небезпека: якщо тест звіряє продуктовий вивід (widget.text(), toolTip()...)
з очікуваним значенням, узятим через виклик `tr("ключ")` (а не через жорсткий
текстовий літерал), то видалений/зламаний i18n-ключ ламає ОБИДВІ сторони
порівняння однаково — продукт показує сирий ключ, tr() у тесті повертає той
самий сирий ключ — і тест лишається зеленим, хоча користувач бачить непереклад.

Два незалежні чеки:

1. `test_no_self_tautology` — САМОГО СЕБЕ тест `tr(X) == tr(X)` (чи
   `assertEqual(tr(X), tr(X))`) з ОДНАКОВИМ ключем X по обидва боки. Це
   буквальний, завжди хибний патерн — жодних винятків, allowlist не потрібен.
   Мутаційна проба координатора (крок 4 завдання) ловиться саме тут.

2. `test_no_new_widget_vs_tr_tautology` — ширший (і м'якший) чек: порівняння
   (`==`/`!=`, або `assertEqual`/`assertNotEqual`), де РІВНО одна сторона —
   виклик `tr(...)`, а інша — НЕ виклик `tr(...)` (типово: `widget.text()`,
   `label.toolTip()`, захоплений мок-аргумент тощо). Такі порівняння вже є в
   репо (частина — з'ясовано і винесено в ALLOWLIST нижче, частина виправлена
   окремо на жорсткі літерали 31.07.2026) — вартовий лише не дає з'явитись
   НОВИМ входженням поза allowlist.

   Свідомо НЕ покриває ідіому `self.assertIn(tr(key), texts)` (і
   `assertNotIn`) — вона зустрічається в tests/ десятки разів (список
   зібраних текстів багатьох віджетів) і потребувала б окремого,
   набагато ширшого allowlist; залишено як відомий пробіл, задокументований
   тут і в звіті координатора.

Суто AST-скан, без Qt і без імпорту застосунку — швидко й без побічних дій.
Запуск вручну:  python -m unittest tests.test_i18n_tautology_lint
"""
import ast
import unittest
from collections import Counter
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_SELF = Path(__file__).resolve()

_EQ_ASSERT_METHODS = {"assertEqual", "assertNotEqual"}

# Існуючі (ще НЕ виправлені на 31.07.2026) входження патерну "widget-вивід
# порівнюється з tr(ключ)" — виявлені живим grep+AST-переглядом координатора.
#
# Ключ винятку: (ім'я файлу, tr-ключ) -> ОЧІКУВАНА кількість входжень у
# файлі. Номери рядків свідомо НЕ входять у ключ — вони дрейфували при
# кожному злитті, що чіпало ці файли, і 31.07 тричі хибно валили гейт
# (латання: 3b02ffc, 152b1b7, d22cbc6). Лічильник — точна рівність:
#   спостережено > очікуваного → з'явилось НОВЕ порівняння з тим самим
#     ключем (можливо, справжня тавтологія) — розберись і, якщо навмисне,
#     збільш лічильник;
#   спостережено < очікуваного → входження зникло/виправлено — зменш або
#     прибери запис, щоб виняток не жив мертвим (саме так 31.07 висіло
#     застаріле ("test_meeting_ui.py", 1398)).
# True замість tr-ключа = перший аргумент tr(...) не текстовий літерал.
#
# Більшість тут — ідіома `[x for x in items if x.text() == tr(key)]` для
# ПОШУКУ віджета з подальшою НЕЗАЛЕЖНОЮ перевіркою (розкладка, стиль, клік →
# побічний ефект) — прийнятний ризик за оцінкою координатора 31.07.2026.
# Решта — прямі assertEqual/assertNotEqual, класифіковані як справжній
# Патерн 2, але залишені за межами критичного пакета цієї сесії (наради +
# захист екрана вже виправлено) через ліміт часу.
_ALLOWLIST = {
    ("render_author_links_smoke.py", "set_tab_about"): 1,
    ("render_brand_smoke.py", "set_tab_about"): 2,
    ("render_layout_smoke.py", "set_open_logs"): 1,
    ("render_meeting_smoke.py", True): 1,
    ("render_meeting_smoke.py", "audioedit_open"): 1,
    ("render_meeting_smoke.py", "audioedit_track_pick"): 1,
    ("render_meeting_smoke.py", "meeting_card_delete"): 1,
    ("render_meeting_smoke.py", "meeting_error_silence"): 2,
    ("render_meeting_smoke.py", "meeting_security_open_tip"): 1,
    ("render_meeting_smoke.py", "meeting_title_save"): 1,
    ("render_nav_smoke.py", "about_open"): 1,
    ("render_nav_smoke.py", "protocol_model_about_name"): 1,
    ("render_nav_smoke.py", "search_open"): 1,
    ("render_nav_smoke.py", "set_tab_about"): 1,
    ("render_report_toast_smoke.py", "about_open"): 1,
    ("render_report_toast_smoke.py", "set_help"): 1,
    ("render_report_toast_smoke.py", "set_report_confirm_ok"): 1,
    ("render_report_toast_smoke.py", "set_report_problem"): 1,
    ("render_report_toast_smoke.py", "set_tab_about"): 1,
    ("render_reverse_dictation_smoke.py", "revdict_correct"): 1,
    ("render_reverse_dictation_smoke.py", "revdict_replay"): 1,
    ("test_a11y_batch.py", "dict_card_delete"): 1,
    ("test_audio_qol.py", "set_mic_good"): 1,
    ("test_audio_qol.py", "set_mic_silence"): 1,
    ("test_audio_qol.py", "set_mic_test"): 3,
    ("test_audio_qol.py", "set_mic_testing"): 1,
    ("test_file_cancel.py", "files_cancel"): 1,
    ("test_file_cancel.py", "files_cancelled_body"): 1,
    ("test_file_cancel.py", "files_retry"): 1,
    ("test_help_guide.py", "tray_help"): 2,
    ("test_models_hub_ui.py", True): 1,
    ("test_redaction.py", "audioedit_redact_failed"): 2,
    ("test_support_links_ui.py", "models_hub_tts_engine_absent"): 1,
    ("test_tts_voice_download_wired.py", "hint_tts_voice_download_unavailable"): 1,
    ("test_tts_voice_download_wired.py", "tts_voice_download"): 2,
    ("test_tts_voice_download_wired.py", "tts_voice_download_done"): 1,
    ("test_tts_voice_manager.py", "tts_voice_custom"): 1,
    ("test_tts_voice_manager.py", "tts_voice_sample"): 1,
    ("test_win_hardening.py", "panic_toast_locked"): 4,
}


def _tr_key(node):
    """Якщо node — виклик tr(...)/i18n.tr(...), повертає рядковий перший
    аргумент (ключ) або True, якщо ключ не є текстовим літералом. Інакше —
    None (не виклик tr)."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    name = func.id if isinstance(func, ast.Name) else (
        func.attr if isinstance(func, ast.Attribute) else None)
    if name != "tr":
        return None
    if node.args and isinstance(node.args[0], ast.Constant) \
            and isinstance(node.args[0].value, str):
        return node.args[0].value
    return True


def _iter_comparison_pairs(tree):
    """Дає (lineno, left, right) для `==`/`!=` (Compare, у т.ч. в filter-і
    list-comprehension) і для assertEqual/assertNotEqual(a, b)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 \
                and isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
            yield node.lineno, node.left, node.comparators[0]
        elif isinstance(node, ast.Call):
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else None
            if attr in _EQ_ASSERT_METHODS and len(node.args) >= 2:
                yield node.lineno, node.args[0], node.args[1]


def _scan(py_path):
    """Повертає (self_tautologies, widget_hits), де widget_hits — список
    (ім'я_файлу, tr-ключ, рядок) УСІХ входжень патерну 2 (без фільтра за
    allowlist — звірка лічильників робиться в самому тесті)."""
    self_tautologies = []
    widget_hits = []
    src = py_path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(py_path))
    for lineno, left, right in _iter_comparison_pairs(tree):
        lkey = _tr_key(left)
        rkey = _tr_key(right)
        if lkey is not None and rkey is not None:
            # обидві сторони — tr(...): тавтологія лише якщо ключ ОДНАКОВИЙ
            # (і відомий текстовим літералом по обидва боки).
            if lkey is not True and lkey == rkey:
                self_tautologies.append(f"{py_path.name}:{lineno}")
        elif lkey is not None or rkey is not None:
            # Одна сторона — tr(...), інша — жорсткий текстовий літерал:
            # це і є БАЖАНИЙ фікс-патерн (tr(key) звіряється з реальним
            # перекладом-константою, продукту тут ніде), а не тавтологія —
            # пропускаємо, allowlist не потрібен.
            other = right if lkey is not None else left
            if isinstance(other, ast.Constant) and isinstance(other.value, str):
                continue
            key = lkey if lkey is not None else rkey
            widget_hits.append((py_path.name, key, lineno))
    return self_tautologies, widget_hits


class TrTautologyLint(unittest.TestCase):
    def test_no_self_tautology(self):
        """tr(X) == tr(X) (той самий ключ по обидва боки) — завжди хибний
        патерн, без винятків. Мутаційна проба (крок 4 завдання координатора)
        мусить впасти саме тут."""
        offenders = []
        for py in sorted(_TESTS_DIR.glob("*.py")):
            if py.resolve() == _SELF:
                continue
            try:
                self_taut, _ = _scan(py)
            except SyntaxError:
                continue
            offenders.extend(self_taut)
        self.assertEqual(
            offenders, [],
            "tr(ключ) порівнюється САМ ІЗ СОБОЮ (той самий ключ по обидва "
            "боки assertEqual/==) — тест не захищає нічого, бо зламаний "
            f"ключ дає однаковий результат по обидва боки: {offenders}",
        )

    def test_no_new_widget_vs_tr_tautology(self):
        """Порівняння widget-виводу з tr(ключ) — Патерн 2 з аудиту
        30.07.2026. Спостережені лічильники (файл, tr-ключ) мусять ТОЧНО
        збігтися з _ALLOWLIST: більше — з'явилось нове входження, менше —
        запис allowlist застарів. Номери рядків у ключі не беруть участі,
        тому дрейф рядків при злиттях тест не валить."""
        observed = Counter()
        lines = {}
        for py in sorted(_TESTS_DIR.glob("*.py")):
            if py.resolve() == _SELF:
                continue
            try:
                _, widget_hits = _scan(py)
            except SyntaxError:
                continue
            for fname, key, lineno in widget_hits:
                observed[(fname, key)] += 1
                lines.setdefault((fname, key), []).append(lineno)

        problems = []
        for loc in sorted(set(observed) | set(_ALLOWLIST),
                          key=lambda x: (x[0], str(x[1]))):
            fname, key = loc
            got = observed.get(loc, 0)
            want = _ALLOWLIST.get(loc, 0)
            if got == want:
                continue
            where = ",".join(str(n) for n in sorted(lines.get(loc, []))) or "-"
            if got > want:
                problems.append(
                    f"{fname} tr({key!r}): {got} входжень замість {want} "
                    f"(рядки {where}) — НОВЕ порівняння продуктового виводу "
                    "з tr(ключ); заміни на жорсткий літерал або, якщо поруч "
                    "є незалежна перевірка, збільш лічильник в _ALLOWLIST")
            else:
                problems.append(
                    f"{fname} tr({key!r}): {got} входжень замість {want} "
                    "— запис _ALLOWLIST застарів, зменш лічильник або "
                    "прибери запис")
        self.assertEqual(
            problems, [],
            "Розбіжність лічильників Патерну 2 з _ALLOWLIST у "
            "tests/test_i18n_tautology_lint.py:\n" + "\n".join(problems),
        )


if __name__ == "__main__":
    unittest.main()
