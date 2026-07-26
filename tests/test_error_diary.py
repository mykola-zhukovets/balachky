"""Юніти мовного щоденника помилок (feature/diary-calendar): агрегатор
whisper_core.error_diary поверх корпусу. Без Qt, без диска — зразки як list dict-ів
(той самий формат, що дає corpus.load_samples), а інтеграцію з диском перевіряємо
через tempfile-корпус."""
import tempfile
import unittest

from whisper_core import corpus, error_diary


def _s(recognized, corrected):
    return {"recognized": recognized, "corrected": corrected}


def _sp(recognized, corrected, profile):
    return {"recognized": recognized, "corrected": corrected, "profile": profile}


class AggregateTests(unittest.TestCase):
    def test_identical_pairs_are_summed(self):
        samples = [
            _s("свцерква", "Свято-Церква"),
            _s("свцерква", "Свято-Церква"),
            _s("свцерква", "Свято-Церква"),
        ]
        rows = error_diary.aggregate(samples=samples)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["was"], "свцерква")
        self.assertEqual(rows[0]["now"], "Свято-Церква")
        self.assertEqual(rows[0]["count"], 3)

    def test_different_pairs_stay_separate(self):
        samples = [
            _s("свцерква", "Свято-Церква"),
            _s("вихователь", "вихователь"),          # без виправлення — пропуск
            _s("ветиринар", "ветеринар"),
            _s("ветиринар", "ветеринар"),
        ]
        rows = error_diary.aggregate(samples=samples)
        counts = {(r["was"], r["now"]): r["count"] for r in rows}
        self.assertEqual(counts[("ветиринар", "ветеринар")], 2)
        self.assertEqual(counts[("свцерква", "Свято-Церква")], 1)
        self.assertNotIn(("вихователь", "вихователь"), counts)

    def test_case_insensitive_grouping(self):
        samples = [
            _s("Свцерква", "Свято"),
            _s("свцерква", "свято"),
        ]
        rows = error_diary.aggregate(samples=samples)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 2)

    def test_skips_empty_and_no_correction(self):
        samples = [
            _s("", "щось"),
            _s("щось", ""),
            _s("однакове", "однакове"),
            _s("  ", "  "),
        ]
        self.assertEqual(error_diary.aggregate(samples=samples), [])

    def test_sorted_by_frequency_desc(self):
        samples = (
            [_s("ка", "КА-точка")] * 2
            + [_s("бэ", "БЕ-точка")] * 5
            + [_s("вэ", "ВЕ-точка")] * 3
        )
        rows = error_diary.aggregate(samples=samples)
        self.assertEqual([r["count"] for r in rows], [5, 3, 2])
        self.assertEqual([r["was"] for r in rows], ["бэ", "вэ", "ка"])

    def test_reads_from_corpus_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus.save_sample("свцерква", "Свято-Церква", root=tmp)
            corpus.save_sample("свцерква", "Свято-Церква", root=tmp)
            rows = error_diary.aggregate(root=tmp)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["count"], 2)


class ProfileFilterTests(unittest.TestCase):
    """feature/selflearn-dict: щоденник помилок фільтрується за словником —
    жодна пара «роботи» не має пропонуватись у «домі» (спека «never become
    one-click suggestions for a selected profile»)."""

    def test_home_diary_cannot_offer_a_work_pair(self):
        # Спека-вимога дослівно: profile-filtered error diary cannot offer a
        # work corpus pair in home.
        samples = [
            _sp("свцерква", "Свято-Церква", "дім"),
            _sp("свцерква", "Свято-Церква", "дім"),
            _sp("ветиринар", "ветеринар", "робота"),
            _sp("ветиринар", "ветеринар", "робота"),
        ]
        home = error_diary.aggregate(samples=samples, profile="дім")
        pairs = {(r["was"], r["now"]) for r in home}
        self.assertIn(("свцерква", "Свято-Церква"), pairs)
        self.assertNotIn(("ветиринар", "ветеринар"), pairs)   # чужа пара — не тут

    def test_work_diary_shows_only_work_pairs(self):
        samples = [
            _sp("свцерква", "Свято-Церква", "дім"),
            _sp("ветиринар", "ветеринар", "робота"),
            _sp("ветиринар", "ветеринар", "робота"),
        ]
        work = error_diary.aggregate(samples=samples, profile="робота")
        self.assertEqual([(r["was"], r["now"], r["count"]) for r in work],
                         [("ветиринар", "ветеринар", 2)])

    def test_legacy_pair_without_profile_never_offered_under_a_profile(self):
        # Старі глобальні зразки (без поля profile) під профіль-фільтр не
        # потрапляють — провенанс невідомий, тож пропонувати їх у конкретному
        # словнику = той самий витік.
        samples = [_s("свцерква", "Свято-Церква")] * 3
        self.assertEqual(error_diary.aggregate(samples=samples, profile="дім"), [])
        # без фільтра (глобальний звіт/A-B) — усе на місці
        self.assertEqual(len(error_diary.aggregate(samples=samples)), 1)

    def test_disk_corpus_is_profile_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus.save_sample("свцерква", "Свято-Церква", profile="дім", root=tmp)
            corpus.save_sample("свцерква", "Свято-Церква", profile="дім", root=tmp)
            corpus.save_sample("ветиринар", "ветеринар", profile="робота", root=tmp)
            corpus.save_sample("ветиринар", "ветеринар", profile="робота", root=tmp)
            home = error_diary.aggregate(root=tmp, profile="дім")
            self.assertEqual([(r["was"], r["now"], r["count"]) for r in home],
                             [("свцерква", "Свято-Церква", 2)])
            # глобальний перегляд (без profile) бачить обидві пари
            self.assertEqual(len(error_diary.aggregate(root=tmp)), 2)


class TopSuggestionsTests(unittest.TestCase):
    def test_only_repeated_are_suggested(self):
        samples = [_s("одн", "Один")] + [_s("двч", "Двічі")] * 2
        picked = error_diary.top_suggestions(samples=samples)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["was"], "двч")

    def test_limit_n(self):
        samples = (
            [_s("чтр", "Чотири")] * 4
            + [_s("три", "Тричі")] * 3
            + [_s("две", "Двічі")] * 2
        )
        picked = error_diary.top_suggestions(samples=samples, n=2)
        self.assertEqual([r["was"] for r in picked], ["чтр", "три"])


if __name__ == "__main__":
    unittest.main()
