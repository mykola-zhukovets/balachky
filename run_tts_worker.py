"""Точка входу окремого TTS-воркер-EXE (balachky-tts-worker.exe, §12.1).

Frozen: `python -m whisper_core.tts.worker` не працює (немає `-m`), тож PyInstaller
пакує ЦЕЙ скрипт окремим EXE з власним Analysis (torch/transformers ізольовані від
GUI-exe). Sidecar.default_worker_command() у frozen вказує саме на цей exe.

`--selftest` — self-check для qa_gate -Frozen: spawn/ping/synthesize через FakeBackend
+ реальний import-probe нативних TTS-DLL (torch/sherpa)."""
import sys

from whisper_core.tts.worker import main, selftest

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    raise SystemExit(main())
