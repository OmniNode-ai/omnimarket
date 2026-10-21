# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-process evidence terminal carries measured trace and timing (OMN-19448).

Only the local port's terminal reaches the projection and bus (OMN-20154).
Missing measurements must remain absent, while a measured zero stays zero.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from omnimarket.events.emit_effect_topic_publisher import EmitEffectTopicPublisher
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegate_skill as handler_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
    ProtocolDelegationDispatchPort,
    _terminal_measure_dispatch_kwargs,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call,
    transport,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)
from omnimarket.routing import delegation_backend_resolution

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_health_cache() -> None:
    """Ensure the transport patch is reached regardless of earlier probes."""
    handler_llm_delegation_call._health_cache.clear()


def _patch_routing(
    monkeypatch: pytest.MonkeyPatch, backends: list[dict[str, object]]
) -> None:
    """Resolve the routing authority off the in-memory fake backend list."""
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: backends,
    )


def _patch_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return a passing non-code answer through the real effect handler."""

    def fake_probe_health(endpoint_url: str, **_: Any) -> bool:
        return True

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
                                "According to Smith (2020) and the theorem in section 3, the tradeoff "
                                "is significant because the evidence shows X; therefore we conclude Y. "
                                "See references [12] for the methodical analysis and the risk profile "
                                "this approach carries in practice."
                            )
                        }
                    }
                ],
                "model": "Qwen3.6-35B-A3B",
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 22,
                    "total_tokens": 33,
                },
            },
            latency_ms=42,
        )

    monkeypatch.setattr(transport, "probe_health", fake_probe_health)
    monkeypatch.setattr(transport, "post_chat_completion", fake_post)


def test_dispatch_threads_trace_and_timing_onto_the_evidence_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatch persists supplied metadata and always measures its execution."""
    db_path = tmp_path / "delegation.sqlite"
    _patch_routing(
        monkeypatch,
        [
            {
                "backend_id": "local-document",
                "endpoint_url": "http://inference.example:8000/v1/chat/completions",
                "model_name": "Qwen3.6-35B-A3B",
                "tier": "local",
                "max_tokens": 65536,
                "timeout_ms": 300000,
                "capabilities": ["document"],
            }
        ],
    )
    _patch_transport(monkeypatch)
    port = LocalDelegationDispatchPort(
        evidence_db_path=db_path, effect_process_boundary=False
    )
    trace_id = uuid4()

    for measured in (True, False):
        correlation_id = uuid4()
        measurements: dict[str, Any] = (
            {"trace_id": trace_id, "queue_wait_ms": 7} if measured else {}
        )
        result = asyncio.run(
            port.dispatch(
                prompt="explain the tradeoff",
                task_type="document",
                correlation_id=correlation_id,
                max_tokens=256,
                source_file_path=None,
                source_session_id=None,
                wait=True,
                execution_timeout_seconds=240,
                terminal_delivery_margin_seconds=60,
                quality_contract_mode="extend_task_class",
                acceptance_criteria=(),
                tenant_id=None,
                **measurements,
            )
        )
        assert result["status"] == "completed"
        assert result["quality_gate_passed"] is True

        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM delegation_events WHERE correlation_id = ?",
                (str(correlation_id),),
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1
        row = rows[0]
        assert row["trace_id"] == (str(trace_id) if measured else None)
        assert row["queue_wait_ms"] == (7 if measured else None)
        assert isinstance(row["execution_ms"], int)
        assert row["execution_ms"] >= 0


def _row(db_path: Path, correlation_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute(
            "SELECT * FROM delegation_events WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
    finally:
        conn.close()
    assert found is not None
    return dict(found)


def _request(
    trace_id: UUID | None = None, published_at: datetime | None = None
) -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt="summarize the diff",
        task_type="document",
        source="claude-code",
        trace_id=trace_id,
        published_at=published_at,
    )


def _project(
    tmp_path: Path,
    *,
    trace_id: UUID | None = None,
    queue_wait_ms: int | None = None,
    started_monotonic: float | None = None,
) -> tuple[dict[str, object], dict[str, Any]]:
    published: list[dict[str, object]] = []

    class _Publisher:
        def publish(self, **event: object) -> bool:
            published.append(event)
            return True

    db_path = tmp_path / "delegation.sqlite"
    port = LocalDelegationDispatchPort(
        evidence_db_path=db_path,
        terminal_publisher=cast(EmitEffectTopicPublisher, _Publisher()),
    )
    correlation_id = uuid4()
    port._project_evidence(
        correlation_id=correlation_id,
        task_type="document",
        endpoint_ref="local",
        model_id="model-local",
        result=ModelLlmDelegationCallResult(
            request_id="req-omn19448",
            success=True,
            content="an answer",
            tokens_in=11,
            tokens_out=22,
            latency_ms=33,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        ),
        prompt="summarize the diff",
        source_session_id=None,
        tenant_id="omninode",
        quality_passed=True,
        failure_message="",
        cost_usd=Decimal("0"),
        baseline_savings=None,
        escalation_count=0,
        attempts=[],
        actual_score=None,
        required_bar=None,
        trace_id=trace_id,
        queue_wait_ms=queue_wait_ms,
        started_monotonic=started_monotonic,
    )
    assert len(published) == 1
    payload = published[0]["payload"]
    assert isinstance(payload, dict)
    return payload, _row(db_path, str(correlation_id))


def test_in_process_evidence_carries_trace_and_measured_timing(tmp_path: Path) -> None:
    trace_id = uuid4()
    payload, row = _project(
        tmp_path,
        trace_id=trace_id,
        queue_wait_ms=0,
        started_monotonic=time.monotonic() - 0.05,
    )
    assert payload["trace_id"] == str(trace_id)
    assert payload["queue_wait_ms"] == 0
    assert isinstance(payload["execution_duration_ms"], int)
    assert payload["execution_duration_ms"] >= 50
    assert row["trace_id"] == str(trace_id)
    assert row["queue_wait_ms"] == 0
    assert row["execution_ms"] >= 50


def test_unmeasured_evidence_fields_are_absent_and_null(tmp_path: Path) -> None:
    payload, row = _project(tmp_path)
    assert not {"trace_id", "queue_wait_ms", "execution_duration_ms"} & set(payload)
    for column in ("trace_id", "queue_wait_ms", "execution_ms"):
        assert row[column] is None


def test_negative_queue_wait_is_clamped_to_zero(tmp_path: Path) -> None:
    payload, row = _project(tmp_path, queue_wait_ms=-20)
    assert payload["queue_wait_ms"] == 0
    assert row["queue_wait_ms"] == 0


def test_terminal_measure_kwargs_require_dispatch_support() -> None:
    def declared(*, trace_id: UUID | None, queue_wait_ms: int | None) -> None:
        pass

    def legacy(*, prompt: str) -> None:
        pass

    def variadic(**kwargs: object) -> None:
        pass

    request = _request(uuid4())
    expected = {"trace_id": request.trace_id, "queue_wait_ms": 0}
    for dispatch in (declared, variadic, LocalDelegationDispatchPort.dispatch):
        assert _terminal_measure_dispatch_kwargs(request, 0, dispatch) == expected
    assert _terminal_measure_dispatch_kwargs(request, 0, legacy) == {}
    assert _terminal_measure_dispatch_kwargs(_request(), None, declared) == {}


def test_terminal_measure_kwargs_check_each_parameter_independently() -> None:
    def trace_only(*, trace_id: UUID | None) -> None:
        pass

    def queue_only(*, queue_wait_ms: int | None) -> None:
        pass

    request = _request(uuid4())
    assert _terminal_measure_dispatch_kwargs(request, 0, trace_only) == {
        "trace_id": request.trace_id
    }
    assert _terminal_measure_dispatch_kwargs(request, 0, queue_only) == {
        "queue_wait_ms": 0
    }


@pytest.mark.parametrize("error", [TypeError, ValueError])
def test_uninspectable_dispatch_receives_no_measure_kwargs(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    def uninspectable(_dispatch: object) -> None:
        raise error("signature unavailable")

    monkeypatch.setattr(handler_module.inspect, "signature", uninspectable)
    assert _terminal_measure_dispatch_kwargs(_request(uuid4()), 0, object) == {}


async def test_handler_passes_trace_and_measured_queue_wait() -> None:
    class _Port:
        def __init__(self) -> None:
            self.received: dict[str, object] = {}

        async def dispatch(
            self,
            *,
            trace_id: UUID | None = None,
            queue_wait_ms: int | None = None,
            **kwargs: object,
        ) -> dict[str, object]:
            self.received = {"trace_id": trace_id, "queue_wait_ms": queue_wait_ms}
            return {"status": "completed", "content": "ok"}

    port = _Port()
    handler = HandlerDelegateSkill(
        object(), dispatch_port=cast(ProtocolDelegationDispatchPort, port)
    )
    request = _request(uuid4(), datetime.now(UTC) - timedelta(seconds=1))
    terminal = await handler.handle(request)
    assert terminal.status == "completed"
    assert port.received["trace_id"] == request.trace_id
    assert isinstance(port.received["queue_wait_ms"], int)
    assert port.received["queue_wait_ms"] >= 0


async def test_handler_keeps_legacy_dispatch_working() -> None:
    class _LegacyPort:
        def __init__(self) -> None:
            self.received: dict[str, object] = {}

        async def dispatch(
            self,
            *,
            prompt: str,
            task_type: str,
            correlation_id: UUID,
            max_tokens: int | None,
            source_file_path: str | None,
            source_session_id: str | None,
            wait: bool,
            execution_timeout_seconds: int,
            terminal_delivery_margin_seconds: int,
            quality_contract_mode: str,
            acceptance_criteria: tuple[str, ...],
            tenant_id: str | None,
            provenance: object = None,
            backend_id: str | None = None,
            response_contract: dict[str, object] | None = None,
            system_prompt: str | None = None,
            temperature: float | None = None,
            response_format: dict[str, object] | None = None,
        ) -> dict[str, object]:
            self.received = {
                key: value for key, value in locals().items() if key != "self"
            }
            return {"status": "completed", "content": "ok"}

    port = _LegacyPort()
    handler = HandlerDelegateSkill(
        object(), dispatch_port=cast(ProtocolDelegationDispatchPort, port)
    )
    request = _request(uuid4(), datetime.now(UTC) - timedelta(seconds=1))
    terminal = await handler.handle(request)
    assert terminal.status == "completed"
    assert port.received["correlation_id"] == request.correlation_id
    assert "trace_id" not in port.received
    assert "queue_wait_ms" not in port.received
