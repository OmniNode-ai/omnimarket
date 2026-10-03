# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18887 AC5: a redelivered command bills once through the real local chain."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.dispatch_envelope_context import bind_dispatch_envelope

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.routing import delegation_backend_resolution


def _delivery(correlation_id: UUID, envelope_id: UUID) -> ModelEventEnvelope[object]:
    """Bind the same envelope ID for redelivery, a new one for a new command."""
    return ModelEventEnvelope[object](
        envelope_id=envelope_id,
        payload={},
        correlation_id=correlation_id,
        envelope_timestamp=datetime.now(UTC),
        event_type="omnimarket.delegate-skill",
        source_tool="omn18887-test",
    )


@pytest.fixture
def local_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[HandlerDelegateSkill, list[dict[str, Any]], Path]:
    """Keep dispatch, judging and SQLite evidence real; fake the HTTP boundary."""
    captured_payloads: list[dict[str, Any]] = []

    def fake_post(
        *,
        endpoint_url: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        extra_headers: dict[str, str] | None = None,
        runtime_profile: str | None = None,
    ) -> transport.ModelTransportResponse:
        # Every HTTP call counts, even with an identical payload. Without the
        # delivery claim, two handler invocations would produce two entries.
        captured_payloads.append(payload)
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

    db_path = tmp_path / "delegation.sqlite"
    port = LocalDelegationDispatchPort(
        evidence_db_path=db_path,
        effect_process_boundary=False,
    )
    return HandlerDelegateSkill(dispatch_port=port), captured_payloads, db_path


@pytest.mark.unit
async def test_a_redelivered_record_calls_the_provider_once(
    local_chain: tuple[HandlerDelegateSkill, list[dict[str, Any]], Path],
) -> None:
    handler, captured_payloads, db_path = local_chain
    correlation_id = uuid4()
    envelope_id = uuid4()
    delivery = _delivery(correlation_id, envelope_id)
    request = ModelDelegateSkillRequest(
        prompt="Write pytest unit tests for normalize_status.",
        task_type="test",
        source="codex",
        correlation_id=correlation_id,
    )

    with bind_dispatch_envelope(delivery):
        first = await handler.handle(request)
    with bind_dispatch_envelope(delivery):
        second = await handler.handle(request)

    assert len(captured_payloads) == 1, "a redelivery must not bill the provider twice"
    assert first.status == "completed"
    assert second.status == "completed"
    assert first.response == second.response

    conn = sqlite3.connect(str(db_path))
    try:
        row_count = conn.execute(
            "SELECT COUNT(*) FROM delegation_events WHERE correlation_id = ?",
            (str(correlation_id),),
        ).fetchone()[0]
    finally:
        conn.close()

    assert row_count == 1


@pytest.mark.unit
async def test_a_reused_correlation_on_a_new_record_still_calls_the_provider(
    local_chain: tuple[HandlerDelegateSkill, list[dict[str, Any]], Path],
) -> None:
    handler, captured_payloads, _ = local_chain
    correlation_id = uuid4()
    first_envelope_id = uuid4()
    second_envelope_id = uuid4()
    request = ModelDelegateSkillRequest(
        prompt="Write pytest unit tests for normalize_status.",
        task_type="test",
        source="codex",
        correlation_id=correlation_id,
    )

    with bind_dispatch_envelope(_delivery(correlation_id, first_envelope_id)):
        first = await handler.handle(request)
    with bind_dispatch_envelope(_delivery(correlation_id, second_envelope_id)):
        second = await handler.handle(request)

    assert len(captured_payloads) == 2, "a new record is a retry, not a redelivery"
    assert first.status == "completed"
    assert second.status == "completed"
