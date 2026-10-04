# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20453: the cached lane secret store must not be driven from two event loops.

``_configured_secret_store`` is memoized for the life of the process, and the
store it returns holds asyncio primitives: ``SecretResolver``'s per-key
``asyncio.Lock`` and ``HandlerInfisical``'s circuit-breaker ``asyncio.Lock``.
An ``asyncio.Lock`` binds to the first event loop that has to wait on it. The
sync entry points of this module (``resolve_api_key``, ``resolve_api_key_loop_safe``,
``resolve_api_key_with_source_loop_safe``) each drive the store with
``asyncio.run``, often on a fresh worker thread, while async callers await it
on the runtime loop. So once one loop has contended a lock, the next loop that
contends it raises ``RuntimeError: ... is bound to a different event loop``.

On the .201 dev lane that was 49 failed tenant delegations in runtime-effects
between 2026-10-03T22:48:14Z and 23:00:12Z. These tests reproduce it with the
real ``SecretResolver`` and a fake Infisical handler whose read yields to the
loop, so concurrent reads of one ref contend the per-key lock.
"""

from __future__ import annotations

import asyncio
import textwrap
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnimarket.inference import secret_store_resolver as ssr
from omnimarket.inference.secret_store_resolver import (
    clear_secret_store_resolver_cache,
    resolve_api_key,
    resolve_api_key_async,
)

_REF = "llm.loopaffinity.api_key"

_LANE_CONFIG_YAML = textwrap.dedent(f"""\
    enable_convention_fallback: false
    mappings:
      - logical_name: {_REF}
        source:
          source_type: infisical
          source_path: /dev/onex-runtime/LLM_LOOPAFFINITY_API_KEY
""")

_BOOTSTRAP_ENV = {
    "INFISICAL_ADDR": "http://infisical.example.invalid:8080",
    "INFISICAL_CLIENT_ID": "id-not-a-secret-value",
    "INFISICAL_CLIENT_SECRET": "unit-test-placeholder",
    "INFISICAL_PROJECT_ID": "e5010c63-94b3-43ed-9554-0d2dcf3c4e36",
    "INFISICAL_ENVIRONMENT_SLUG": "dev",
}


class _SlowMissInfisicalHandler:
    """Async read that yields to the loop and finds nothing.

    A miss is never cached by ``SecretResolver``, so every read takes the
    per-key lock; the sleep makes concurrent reads of one ref wait on it,
    which is what binds the lock to a loop.
    """

    def __init__(self) -> None:
        self.read_loops: list[asyncio.AbstractEventLoop] = []
        self._guard = threading.Lock()

    async def execute(self, envelope: dict[str, Any]) -> Any:
        del envelope
        loop = asyncio.get_running_loop()
        with self._guard:
            self.read_loops.append(loop)
        await asyncio.sleep(0.005)
        return SimpleNamespace(result={})


@pytest.fixture
def slow_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_SlowMissInfisicalHandler]:
    for name in ("INFISICAL_REQUIRED", "ONEX_SECRET_RESOLVER_CONFIG_JSON"):
        monkeypatch.delenv(name, raising=False)
    for name, value in _BOOTSTRAP_ENV.items():
        monkeypatch.setenv(name, value)
    config_file = tmp_path / "secret_resolver.yaml"
    config_file.write_text(_LANE_CONFIG_YAML, encoding="utf-8")
    monkeypatch.setenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", str(config_file))

    handler = _SlowMissInfisicalHandler()

    def _fake_build(config: Any) -> _SlowMissInfisicalHandler:
        del config
        return handler

    monkeypatch.setattr(ssr, "_build_infisical_handler", _fake_build)
    clear_secret_store_resolver_cache()
    yield handler
    clear_secret_store_resolver_cache()


async def _burst(width: int = 8) -> list[Any]:
    return list(
        await asyncio.gather(
            *(resolve_api_key_async(_REF, required=False) for _ in range(width))
        )
    )


@pytest.mark.unit
def test_loop_affinity_two_asyncio_run_bursts_on_the_cached_store(
    slow_handler: _SlowMissInfisicalHandler,
) -> None:
    """Two loops in turn, each contending the same ref, both resolve.

    Fails before the fix: the second burst raises ``RuntimeError`` because
    the first burst bound the per-key lock to its own (now closed) loop.
    """
    assert asyncio.run(_burst()) == [None] * 8
    assert asyncio.run(_burst()) == [None] * 8
    assert len(slow_handler.read_loops) == 16


@pytest.mark.unit
def test_loop_affinity_runtime_loop_and_sync_threads_concurrently(
    slow_handler: _SlowMissInfisicalHandler,
) -> None:
    """An async caller on one loop and sync callers on worker threads at once.

    This is the runtime-effects shape: handlers awaiting on the runtime loop
    while sync handlers resolve through ``asyncio.run`` on their own threads.
    """
    errors: list[BaseException] = []

    def _sync_caller() -> None:
        try:
            for _ in range(4):
                assert resolve_api_key(_REF, required=False) is None
        except BaseException as exc:
            errors.append(exc)

    async def _runtime_loop() -> None:
        threads = [threading.Thread(target=_sync_caller) for _ in range(4)]
        for thread in threads:
            thread.start()
        for _ in range(4):
            await _burst()
        for thread in threads:
            await asyncio.to_thread(thread.join, 10)

    asyncio.run(_runtime_loop())
    assert errors == []


@pytest.mark.unit
def test_loop_affinity_every_store_read_runs_on_one_owner_loop(
    slow_handler: _SlowMissInfisicalHandler,
) -> None:
    """Every read of the cached store runs on a single loop that no caller owns."""
    caller_loops: list[asyncio.AbstractEventLoop] = []

    async def _call() -> None:
        caller_loops.append(asyncio.get_running_loop())
        await _burst(2)

    asyncio.run(_call())
    asyncio.run(_call())
    assert resolve_api_key(_REF, required=False) is None

    owner_loops = {id(loop) for loop in slow_handler.read_loops}
    assert len(owner_loops) == 1
    assert owner_loops.isdisjoint({id(loop) for loop in caller_loops})
