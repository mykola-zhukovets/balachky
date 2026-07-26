"""Протокол-воркер: прогрів важких імпортів (клас import-lock deadlock),
структурний контракт chat-completion (приборкання thinking) і dev-selftest.

Каркас: збірка НЕ потрібна для зелені — dev-режимний selftest перевіряється завжди;
frozen-EXE-гілка пропускається, якщо dist/balachky-protocol-worker.exe не зібрано."""
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path

from whisper_core.protocol import (ENV_FAKE_BACKEND, MSG_PONG, MSG_RESPONSE)
from whisper_core.protocol.worker import (_WARMUP_MODULES, _handle,
                                          _warm_engine_imports, LlamaBackend)

ROOT = Path(__file__).resolve().parent.parent


class TestWarmupImports(unittest.TestCase):
    def test_warms_expected_modules_in_order(self):
        calls = []
        warmed = _warm_engine_imports(import_module=calls.append)
        self.assertEqual(calls, list(_WARMUP_MODULES))
        self.assertEqual(warmed, list(_WARMUP_MODULES))
        # llama_cpp має бути серед прогрітих (frozen deadlock — саме на ньому)
        self.assertIn("llama_cpp", _WARMUP_MODULES)

    def test_tolerant_to_missing_module(self):
        def imp(name):
            if name == "llama_cpp":
                raise ImportError("no llama in this venv")
        warmed = _warm_engine_imports(import_module=imp)
        self.assertNotIn("llama_cpp", warmed)      # пропущено, без винятку

    def test_logs_skips(self):
        logs = []
        _warm_engine_imports(import_module=lambda n: (_ for _ in ()).throw(
            ImportError("x")), log=logs.append)
        self.assertTrue(any("skip" in m for m in logs))


class TestChatCompletionContract(unittest.TestCase):
    """LlamaBackend мусить іти через create_chat_completion (штатний chat-шлях
    Gemma → thinking off), а НЕ через голий create_completion (де модель зривається
    в багатотисячний ланцюг міркувань). Перевіряємо структурно, без реальної моделі."""

    def test_generate_uses_chat_completion_single_user_turn(self):
        captured = {}

        class _FakeLlama:
            def __init__(self, **kw):
                captured["init"] = kw

            def create_chat_completion(self, *, messages, max_tokens, temperature):
                captured["messages"] = messages
                captured["max_tokens"] = max_tokens
                return {"choices": [{"message": {"content": "## Підсумок\nОк"}}]}

            def create_completion(self, *a, **k):    # НЕ має викликатись
                raise AssertionError("create_completion не має використовуватись")

        backend = LlamaBackend()
        fake_mod = types.SimpleNamespace(Llama=_FakeLlama)
        sys.modules["llama_cpp"] = fake_mod
        try:
            # обходимо перевірку файлу: підмінюємо _ensure напряму
            backend._model = _FakeLlama()
            backend._key = ("m", 8192, 0)
            out = backend.generate("промт", n_ctx=8192, max_tokens=2048,
                                   temperature=0.2, model_path="m", n_gpu_layers=0)
        finally:
            sys.modules.pop("llama_cpp", None)
        self.assertEqual(out, "## Підсумок\nОк")
        self.assertEqual(captured["messages"], [{"role": "user", "content": "промт"}])

    def test_ensure_caches_by_path_ctx_gpu(self):
        backend = LlamaBackend()
        sentinel = object()
        backend._model = sentinel
        backend._key = ("m", 8192, 0)
        # той самий ключ → без перезавантаження
        self.assertIs(backend._ensure("m", 8192, 0), sentinel)


class TestDevSelftest(unittest.TestCase):
    """dev-режим: run_protocol_worker.py --selftest робить ping+generate через
    FakeBackend і рапортує наявність llama_cpp."""

    def test_dev_selftest_passes(self):
        env = dict(os.environ, **{ENV_FAKE_BACKEND: "1"})
        out = subprocess.run(
            [sys.executable, str(ROOT / "run_protocol_worker.py"), "--selftest"],
            capture_output=True, text=True, timeout=120, env=env,
            cwd=str(ROOT))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("SELFTEST PASS", out.stdout)


class TestIpcHandlers(unittest.TestCase):
    def test_ping_and_generate_via_fake_backend(self):
        os.environ.setdefault(ENV_FAKE_BACKEND, "1")
        from whisper_core.protocol.worker import _make_backend
        backend = _make_backend()
        self.assertEqual(_handle({"type": "ping"}, backend), {"type": MSG_PONG})
        resp = _handle({"type": "generate", "id": "1", "prompt": "x",
                        "model_path": ""}, backend)
        self.assertEqual(resp["type"], MSG_RESPONSE)


if __name__ == "__main__":
    unittest.main()
