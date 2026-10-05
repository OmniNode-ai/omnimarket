# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20383 AC1: every terminal carries the id of the command that delivered it.

Correlation is the retry identity and callers reuse it, so two commands sharing a
correlation produced terminals nobody could tell apart, and neither could a served
replay be told from a second run. The OMN-18887 claim already keys on the
delivering record's id; the terminal now carries that same id as ``command_id``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.dispatch_envelope_context import bind_dispatch_envelope

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
    ProtocolDelegationDispatchPort,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_delegation_claim import (
    ModelDelegationClaimOutcome,
    ModelDelegationTerminalOutcome,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.routing import delegation_backend_resolution

pytestmark = pytest.mark.unit


def _delivery(correlation_id: UUID, envelope_id: UUID) -> ModelEventEnvelope[object]:
    """One delivered record: a redelivery carries the same id, a new command a new one."""
    return ModelEventEnvelope[object](
        envelope_id=envelope_id,
        payload={},
        correlation_id=correlation_id,
        envelope_timestamp=datetime.now(UTC),
        event_type="omnimarket.delegate-skill",
        source_tool="omn20383-test",
    )


def _request(correlation_id: UUID) -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="Write pytest unit tests for normalize_status.",
        task_type="test",
        source="codex",
        correlation_id=correlation_id,
    )


@pytest.fixture
def completing_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> HandlerDelegateSkill:
    """The real local chain with the HTTP boundary faked, so the terminal completes."""

    def fake_post(
        *,
        endpoint_url: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        extra_headers: dict[str, str] | None = None,
        runtime_profile: str | None = None,
    ) -> transport.ModelTransportResponse:
        return transport.ModelTransportResponse(
            status_code=200,
            json_body={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "### ANSWER\n"
                                "import pytest\n\n"
                                "@pytest.mark.unit\n"
                                "def test_normalize_status_ok():\n"
                                "    assert normalize_status('OK') == 'ok'\n"
                            )
                        }
                    }
                ],
                "model": "Qwen3-Coder-30B",
                "usage": {
                    "prompt_tokens": 18,
                    "completion_tokens": 44,
                    "total_tokens": 62,
                },
            },
            latency_ms=37,
        )

    monkeypatch.setattr(transport, "probe_health", lambda *_a, **_k: True)
    monkeypatch.setattr(transport, "post_chat_completion", fake_post)
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: [
            {
                "backend_id": "local-coder",
                "endpoint_url": "http://inference.example:8000/v1/chat/completions",
                "model_name": "Qwen3-Coder-30B",
                "tier": "local",
                "max_tokens": 65536,
                "timeout_ms": 300000,
                "capabilities": ["test", "code_generation"],
            }
        ],
    )
    port = LocalDelegationDispatchPort(
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )
    return HandlerDelegateSkill(dispatch_port=port)


class _RaisingDispatchPort:
    """A dispatch that fails, so the handler builds the failed terminal."""

    async def dispatch(self, **_kwargs: Any) -> dict[str, object]:
        raise RuntimeError("provider unreachable")


def _raising_port() -> ProtocolDelegationDispatchPort:
    return cast(ProtocolDelegationDispatchPort, _RaisingDispatchPort())


class _MemoryClaimPort:
    """The durable claim store, in memory: one row per delivery."""

    def __init__(self) -> None:
        self.rows: dict[UUID, dict[str, object] | None] = {}

    def claim(
        self,
        *,
        delivery_id: UUID,
        correlation_id: UUID,
        reap_context: object | None = None,
    ) -> ModelDelegationClaimOutcome:
        if delivery_id not in self.rows:
            self.rows[delivery_id] = None
            return ModelDelegationClaimOutcome(won=True)
        return ModelDelegationClaimOutcome(
            won=False, served_terminal=self.rows[delivery_id]
        )

    def record_terminal(
        self, *, delivery_id: UUID, terminal: dict[str, object]
    ) -> ModelDelegationTerminalOutcome:
        self.rows[delivery_id] = terminal
        return ModelDelegationTerminalOutcome(won=True, held=None)


async def test_two_commands_sharing_a_correlation_carry_different_command_ids(
    completing_handler: HandlerDelegateSkill,
) -> None:
    correlation_id = uuid4()
    first_id, second_id = uuid4(), uuid4()
    request = _request(correlation_id)

    with bind_dispatch_envelope(_delivery(correlation_id, first_id)):
        first = await completing_handler.handle(request)
    with bind_dispatch_envelope(_delivery(correlation_id, second_id)):
        second = await completing_handler.handle(request)

    assert isinstance(first, ModelDelegateSkillCompleted)
    assert isinstance(second, ModelDelegateSkillCompleted)
    assert first.correlation_id == second.correlation_id == correlation_id
    assert first.command_id == first_id
    assert second.command_id == second_id
    assert first.command_id != second.command_id


async def test_a_served_replay_keeps_its_command_id(
    completing_handler: HandlerDelegateSkill,
) -> None:
    correlation_id = uuid4()
    command_id = uuid4()
    request = _request(correlation_id)
    delivery = _delivery(correlation_id, command_id)

    with bind_dispatch_envelope(delivery):
        original = await completing_handler.handle(request)
    with bind_dispatch_envelope(delivery):
        replayed = await completing_handler.handle(request)

    assert original.command_id == command_id
    assert replayed.command_id == command_id
    assert replayed.model_dump(mode="json")["command_id"] == str(command_id)


async def test_a_failed_terminal_carries_the_command_id() -> None:
    correlation_id = uuid4()
    first_id, second_id = uuid4(), uuid4()
    request = _request(correlation_id)
    handler = HandlerDelegateSkill(
        dispatch_port=_raising_port(),
        idempotency_port=_MemoryClaimPort(),
    )

    with bind_dispatch_envelope(_delivery(correlation_id, first_id)):
        first = await handler.handle(request)
    with bind_dispatch_envelope(_delivery(correlation_id, second_id)):
        second = await handler.handle(request)
        replayed_second = await handler.handle(request)

    assert isinstance(first, ModelDelegateSkillFailed)
    assert isinstance(second, ModelDelegateSkillFailed)
    assert first.command_id == first_id
    assert second.command_id == second_id
    # The replay inside the second delivery is that delivery's own terminal.
    assert replayed_second.command_id == second_id


async def test_a_failed_replay_keeps_the_original_command_id() -> None:
    correlation_id = uuid4()
    command_id = uuid4()
    request = _request(correlation_id)
    claims = _MemoryClaimPort()
    delivery = _delivery(correlation_id, command_id)

    with bind_dispatch_envelope(delivery):
        original = await HandlerDelegateSkill(
            dispatch_port=_raising_port(),
            idempotency_port=claims,
        ).handle(request)
        replayed = await HandlerDelegateSkill(
            dispatch_port=_raising_port(),
            idempotency_port=claims,
        ).handle(request)

    assert original.command_id == command_id
    assert replayed.command_id == command_id


async def test_a_direct_call_without_a_delivery_has_no_command_id() -> None:
    """No bound envelope means no delivering command: the id is absent, not faked."""
    handler = HandlerDelegateSkill(
        dispatch_port=_raising_port(),
        idempotency_port=_MemoryClaimPort(),
    )

    terminal = await handler.handle(_request(uuid4()))

    assert terminal.command_id is None
    assert "command_id" not in terminal.model_dump(mode="json")
