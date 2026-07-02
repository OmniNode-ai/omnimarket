# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13842: local delegation effect must be fork-safe on macOS.

The bus-less ``onex delegate`` path runs the blocking LLM effect behind a
supervised child-process boundary (OMN-13597). The child was started with the
``fork`` start method. On macOS the delegation effect makes a real LLM call
(health probe + curl / httpx POST) that initializes the Objective-C runtime in
the parent (Foundation / CoreFoundation proxy resolution, TLS). Executing any
objc call in a ``fork``-ed child after the runtime is live aborts the child with
SIGABRT:

    objc[...]: +[NSNumber initialize] may have been in progress in another thread
    when fork() was called. ... We cannot safely call it or ignore it in the
    fork() child process. Crashing instead.

The child then exits ``-6`` and ``dispatch()`` raised
``delegation effect process exited without returning a result`` — the skill
returned no typed receipt (classified ``broken`` in the M5 dogfood sweep).

Fix: start the effect child with ``spawn`` on macOS (a clean interpreter with no
inherited objc state), keeping ``fork`` elsewhere. These tests prove:

1. ``_resolve_effect_process_context`` selects ``spawn`` on ``darwin`` and
   ``fork`` on Linux (the platform-specific start-method decision).
2. The process boundary still returns a typed result dict through a real spawned
   child on the host platform — no crash, no raised ``RuntimeError``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
    _resolve_effect_process_context,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing import delegation_backend_resolution


class SuccessfulEffectHandler:
    """Pickleable effect double returning a canned success result.

    Module-level (importable) so it survives the ``spawn`` pickling round-trip.
    """

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content="hello",
            output_hash="omn13842-output-hash",
            tokens_in=3,
            tokens_out=1,
            latency_ms=10,
        )


@pytest.fixture
def fake_backends() -> list[dict[str, object]]:
    return [
        {
            "backend_id": "local-coder",
            "endpoint_url": "http://unreachable.invalid:8000/v1/chat/completions",
            "model_name": "Qwen3.6-35B-A3B",
            "tier": "local",
            "max_tokens": 65536,
            "timeout_ms": 500,
            "capabilities": ["research"],
        }
    ]


def _patch_routing(
    monkeypatch: pytest.MonkeyPatch, backends: list[dict[str, object]]
) -> None:
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: backends,
    )


def test_darwin_selects_spawn_start_method(monkeypatch: pytest.MonkeyPatch) -> None:
    """On macOS the effect child must use ``spawn`` (fork aborts via objc SIGABRT)."""
    monkeypatch.setattr(port_module.sys, "platform", "darwin")
    context = _resolve_effect_process_context()
    assert context.get_start_method() == "spawn"


def test_linux_keeps_fork_start_method(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Linux ``fork`` is retained (objc fork-safety does not apply there)."""
    monkeypatch.setattr(port_module.sys, "platform", "linux")
    context = _resolve_effect_process_context()
    assert context.get_start_method() == "fork"


def test_dispatch_returns_typed_receipt_through_process_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_backends: list[dict[str, object]],
) -> None:
    """The supervised child returns a typed result on the host platform.

    Proves the fork-safety fix end-to-end: the effect runs in a real spawned (on
    macOS) child, returns its result over the queue, and ``dispatch()`` yields a
    typed terminal dict — never a ``-6`` crash / ``exited without returning a
    result`` RuntimeError. The effect double is pickleable so the ``spawn``
    round-trip succeeds.
    """
    db_path = tmp_path / "delegation.sqlite"
    _patch_routing(monkeypatch, fake_backends)

    port = LocalDelegationDispatchPort(
        effect_handler=SuccessfulEffectHandler(),
        evidence_db_path=db_path,
    )

    async def _run() -> dict[str, object]:
        return await asyncio.wait_for(
            port.dispatch(
                prompt="say hello",
                task_type="research",
                correlation_id=uuid4(),
                max_tokens=16,
                source_file_path=None,
                source_session_id=None,
                wait=True,
                quality_contract_mode="extend_task_class",
                acceptance_criteria=(),
            ),
            timeout=30.0,
        )

    result = asyncio.run(_run())

    # Typed terminal receipt — a real dict with a known terminal status, not a
    # crash. The success content flows back through the process boundary.
    assert isinstance(result, dict)
    assert result["status"] in {"completed", "failed"}
    assert result["content"] == "hello"
    assert result["model_name"] == "Qwen3.6-35B-A3B"
