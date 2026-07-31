"""Регрес 31.07: тестовий/offscreen екземпляр НЕ повинен займати робочий
канал single-instance "balachky-single". Живий випадок — власник запустив
run_app.py, поки на машині біг unittest discover (тест
test_onboarding_skip.py викликає fronts.desktop.app.main(), яке піднімає
справжній QLocalServer), і отримав хибне «вже запущені» від невидимого
тестового процесу.

Фікс: fronts.desktop.app._instance_channel_name() додає до літералу
"balachky-single" суфікс з BALACHKY_INSTANCE_SUFFIX (виставляє
tests/_isolation.py для кожного тестового процесу). Тут перевіряємо саму
властивість ізоляції на рівні QLocalServer/QLocalSocket, без підняття
повного main() (той шлях уже вкритий test_onboarding_skip.py).
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from fronts.desktop.app import _instance_channel_name


class SingleInstanceChannelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # QApplication (не QCoreApplication): інші тестові файли в тому ж
        # процесі очікують GUI-синглтон (напр. QApplication.setQuitOnLastWindowClosed
        # у main()) — QCoreApplication.instance() "затуляє" його на весь процес.
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._orig_suffix = os.environ.get("BALACHKY_INSTANCE_SUFFIX")
        self._servers = []

    def tearDown(self):
        for server in self._servers:
            name = server.serverName()
            server.close()
            QLocalServer.removeServer(name)
        if self._orig_suffix is None:
            os.environ.pop("BALACHKY_INSTANCE_SUFFIX", None)
        else:
            os.environ["BALACHKY_INSTANCE_SUFFIX"] = self._orig_suffix

    def _listen(self, channel: str) -> QLocalServer:
        QLocalServer.removeServer(channel)
        server = QLocalServer()
        self.assertTrue(server.listen(channel), server.errorString())
        self._servers.append(server)
        return server

    def _probe_connects(self, channel: str) -> bool:
        sock = QLocalSocket()
        sock.connectToServer(channel)
        alive = sock.waitForConnected(300)
        sock.abort()
        return alive

    def test_product_channel_literal_unchanged_without_env(self):
        """Без BALACHKY_INSTANCE_SUFFIX ім'я каналу — байт-у-байт як зараз.

        Літерал у тесті (не через _instance_channel_name повторно), щоб
        зловити випадкове перейменування каналу: воно зламало б
        single-instance при оновленні поверх старої версії застосунку."""
        os.environ.pop("BALACHKY_INSTANCE_SUFFIX", None)
        self.assertEqual(_instance_channel_name(), "balachky-single")

    def test_different_suffixes_do_not_see_each_other(self):
        os.environ["BALACHKY_INSTANCE_SUFFIX"] = "-instance-a"
        channel_a = _instance_channel_name()
        self._listen(channel_a)

        os.environ["BALACHKY_INSTANCE_SUFFIX"] = "-instance-b"
        channel_b = _instance_channel_name()
        self.assertNotEqual(channel_a, channel_b)

        # "екземпляр Б" пробує канал під власним іменем — сервера там нема,
        # хоча "екземпляр А" живий на своєму каналі.
        self.assertFalse(self._probe_connects(channel_b))

    def test_same_suffix_second_instance_detects_first(self):
        os.environ["BALACHKY_INSTANCE_SUFFIX"] = "-instance-shared"
        channel = _instance_channel_name()
        self._listen(channel)

        # "другий екземпляр" з тим самим суфіксом бачить перший — саме так
        # продукт сьогодні ловить подвійний запуск.
        self.assertTrue(self._probe_connects(channel))


if __name__ == "__main__":
    unittest.main()
