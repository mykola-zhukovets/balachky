"""Вимірювання оперативної памʼяті мусить працювати у зібраній програмі.

Знахідка 25.07: `import psutil` стоїть усередині try/except у тілі функції
`heavy_models._default_total_ram`, тому модульний граф PyInstaller його не бачив
і в збірку пакет не потрапляв — перевірено по build/balachky/Analysis-00.toc і
по dist/Balachky (жодного psutil). На машині розробника все працювало, бо там
пакет установлений у середовищі.

Ціна в релізі невидима, але реальна: без psutil захист памʼяті працює
fail-closed, тобто програма на будь-якому залізі вважає, що памʼяті мало, і не
тримає розпізнавання та озвучення резидентно разом. Людина з 64 ГБ отримувала
поведінку машини з 8 ГБ — щоразу вивантаження й завантаження моделі.

Тут два замки: пакет мусить лишатись у переліку для збірки, і поведінка без
нього мусить лишатись безпечною (нижче порога), а не оптимістичною.
"""
import unittest
from pathlib import Path
from unittest import mock

from whisper_core import heavy_models


class RamProbeInBuildTests(unittest.TestCase):
    def test_psutil_listed_for_frozen_build(self):
        """psutil у hiddenimports — інакше в зібраній програмі його не буде."""
        spec = (Path(__file__).resolve().parents[1] / "balachky.spec").read_text(encoding="utf-8")
        head = spec.split("hiddenimports=hiddenimports")[0]
        self.assertIn('"psutil"', head,
                      "psutil зник із hiddenimports — у збірці вимірювання памʼяті "
                      "знову не працюватиме, і програма поводитиметься як на 8 ГБ")

    def test_probe_reads_real_memory_when_available(self):
        """Коли пакет є, беремо справжнє число, а не поріг."""
        fake = mock.Mock()
        fake.virtual_memory.return_value = mock.Mock(total=64 * 1024 ** 3)
        with mock.patch.dict("sys.modules", {"psutil": fake}):
            self.assertEqual(heavy_models._default_total_ram(), 64 * 1024 ** 3)

    def test_probe_stays_fail_closed_without_package(self):
        """Пакета немає — віддаємо значення НИЖЧЕ порога (обережний режим)."""
        with mock.patch.dict("sys.modules", {"psutil": None}):
            value = heavy_models._default_total_ram()
        self.assertLess(value, heavy_models._DEFAULT_RESIDENT_THRESHOLD,
                        "без вимірювання памʼяті захист мусить бути обережним, "
                        "а не вважати, що памʼяті вдосталь")


if __name__ == "__main__":
    unittest.main()
