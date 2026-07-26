"""Точка входу окремого протокол-воркер-EXE (balachky-protocol-worker.exe).

Frozen: `python -m whisper_core.protocol.worker` не працює (немає `-m`), а запуск
воркера через головний Balachky.exe непридатний — він windowed (console=False), тож
sys.stdin/stdout=None і IPC по пайпах не піднявся б (той самий урок, що з TTS-воркером,
§12.1). Тому PyInstaller пакує ЦЕЙ скрипт окремим console=True EXE із власним Analysis
(llama-cpp-python ізольований від GUI-exe). Sidecar.default_worker_command() у frozen
вказує саме на цей exe.
"""
import sys

from whisper_core.protocol.worker import main, selftest

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    raise SystemExit(main())
