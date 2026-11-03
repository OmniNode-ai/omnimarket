# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22224: a deployed-lane delegation run reaches run_trace, and every attempt has a duration.

AC1 drives the steps the deployed lane's own port derives from a terminal
(``_record_bus_run`` into the port's buffered publisher), wraps each one in the
envelope the port puts on the bus, unwraps it the way the standalone writer's
runner does, and hands it to the writer's ``project_event`` against a stand-in
pool that keeps rows by (tenant, run, sequence). No database is read or written.

AC2 builds the per-attempt record the way the dispatch builds it and asserts the
wire record carries ``duration_ms`` on every rung, including a rung that was
never sent (0) and an earlier model of one customer route (measured by the
effect that made the call).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from omnimarket.models.delegation.model_earlier_model_attempt import (
    ModelEarlierModelAttempt,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
    _BufferedRunStepPublisher,
    _record_bus_run,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.run_step_recorder import (
    RunStepRecorder,
)
from omnimarket.nodes.node_projection_run_trace.handlers.handler_run_trace_writer import (
    RunTraceProjectionWriter,
)
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.runner import MessageMeta

_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
_RUN = UUID("5b0b6c1e-3f52-4f0e-9a44-2f1f5d6f7a10")
_TOPIC = "onex.evt.omnimarket.delegation-run-step.v1"
_COLUMNS = (
    "tenant_id",
    "correlation_id",
    "step_sequence",
    "step_kind",
    "started_at",
    "duration_ms",
    "outcome",
    "failure_cause",
    "backend_id",
    "model_id",
    "attempt_number",
    "reason",
    "candidates",
    "failing_step_sequence",
)


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Connection:
    def __init__(self, rows: dict[tuple[str, str, int], dict[str, Any]]) -> None:
        self._rows = rows

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, *_args: object) -> None:
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "tenant_uuid = $1" in sql and str(args[0]) == _TENANT:
            return args[0]
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        key = (str(args[0]), str(args[1]), int(args[2]))
        if "INSERT INTO public.run_trace" in sql:
            if key in self._rows:
                return []
            row = dict(zip(_COLUMNS, args, strict=True))
            row["updated_at"] = datetime(2026, 11, 3, tzinfo=UTC)
            row["projection_cursor"] = len(self._rows) + 1
            self._rows[key] = row
            return [dict(row)]
        return [dict(self._rows[key])] if key in self._rows else []


class _Acquire:
    def __init__(self, conn: _Connection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Connection:
        return self._conn

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Pool:
    def __init__(self, conn: _Connection) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


class _Db:
    def __init__(self, conn: _Connection) -> None:
        self.pool = _Pool(conn)
        self.fetchval = conn.fetchval


def _writer(
    rows: dict[tuple[str, str, int], dict[str, Any]],
) -> RunTraceProjectionWriter:
    stand_in = _Db(_Connection(rows))

    class _Writer(RunTraceProjectionWriter):
        @property
        def db(self) -> Any:
            return stand_in

        def db_for(self, table: str, *, operation: str = "write") -> Any:
            return stand_in

        async def publish_snapshot_delta(self, exposure: Any, **kwargs: Any) -> bool:
            return True

    return _Writer()


def _deployed_terminal() -> dict[str, object]:
    """A deployed run that climbed one rung: two attempts, both timed by the chain."""
    return {
        "tenant_id": _TENANT,
        "backend_ref": "cheap-cloud",
        "model_used": "model-b",
        "cost_tier_name": "cheap_cloud",
        "latency_ms": 910,
        "escalation_history": [
            {
                "backend_ref": "local-qwen",
                "model_used": "model-a",
                "tier": "local",
                "latency_ms": 2140,
                "acceptance_decision": "climb",
                "acceptance_reason": "quality_gate_rejection",
                "failure_reasons": ["too short"],
            },
            {
                "backend_ref": "cheap-cloud",
                "model_used": "model-b",
                "tier": "cheap_cloud",
                "latency_ms": 910,
                "acceptance_decision": "accept",
            },
        ],
    }


async def test_deployed_run_steps_projected() -> None:
    started_at = datetime(2026, 11, 3, 1, 0, tzinfo=UTC)
    buffer = _BufferedRunStepPublisher()
    recorder = RunStepRecorder(
        tenant_id=_TENANT,
        correlation_id=_RUN,
        publisher=buffer,
        store=None,
        started_at=started_at,
        started_monotonic=0.0,
    )
    _record_bus_run(
        recorder,
        task_type="code_generation",
        status="completed",
        flat=_deployed_terminal(),
        started_at=started_at,
    )
    assert buffer.pending, "the deployed lane derived no run steps"
    assert {step.topic for step in buffer.pending} == {_TOPIC}

    rows: dict[tuple[str, str, int], dict[str, Any]] = {}
    writer = _writer(rows)
    assert writer.subscribe_topics == [_TOPIC], (
        "the run-trace writer is not subscribed to the topic the deployed lane "
        "publishes run steps on; its contract.yaml event_bus.subscribe_topics "
        "must name it"
    )
    for offset, step in enumerate(buffer.pending):
        wire = json.dumps({"event_type": step.event_type, "payload": step.payload})
        data = unwrap_envelope(wire.encode("utf-8"))
        assert data is not None
        meta = MessageMeta(
            partition=0, offset=offset, fallback_id=f"run-step-0-{offset}", topic=_TOPIC
        )
        assert await writer.project_event(_TOPIC, data, meta) is True

    stored = sorted(rows.values(), key=lambda row: row["step_sequence"])
    assert [row["step_sequence"] for row in stored] == list(
        range(1, len(buffer.pending) + 1)
    )
    assert {row["tenant_id"] for row in stored} == {_TENANT}
    assert [row["step_kind"] for row in stored] == [
        "request",
        "routing_decision",
        "provider_call",
        "quality_gate",
        "routing_decision",
        "provider_call",
        "quality_gate",
        "terminal",
    ]


def test_attempt_has_duration_on_the_wire_record() -> None:
    assert "duration_ms" in ModelDelegateSkillAttemptRecord.model_fields, (
        "the per-rung attempt record declares no duration, so per-rung time "
        "cannot be split from a run with two or more attempts"
    )
    record = ModelDelegateSkillAttemptRecord(
        tier="local",
        backend_id="local-qwen",
        model_id="model-a",
        quality_gate_passed=False,
        duration_ms=2140,
    )
    assert record.duration_ms == 2140
    with pytest.raises(ValueError, match="duration_ms"):
        ModelDelegateSkillAttemptRecord(
            tier="local",
            backend_id="local-qwen",
            model_id="model-a",
            quality_gate_passed=False,
            duration_ms=-1,
        )


def test_attempt_has_duration_on_an_earlier_model_of_one_route() -> None:
    assert "duration_ms" in ModelEarlierModelAttempt.model_fields, (
        "the first model of a customer route is a rung of its own and is "
        "recorded with no duration"
    )


def test_attempt_has_duration_at_every_dispatch_site() -> None:
    """Every rung the local dispatch records names its duration, even one never sent."""
    import ast
    import inspect

    from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
        port_local_delegation_dispatch as port,
    )

    tree = ast.parse(inspect.getsource(port))
    missing: list[int] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "attempts"
            and node.args
            and isinstance(node.args[0], ast.Dict)
        ):
            continue
        keys = {
            key.value
            for key in node.args[0].keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        if "duration_ms" not in keys:
            missing.append(node.lineno)
    assert not missing, f"attempt records built without duration_ms at lines {missing}"


def test_attempt_has_duration_from_the_port_record_and_the_bus_ladder() -> None:
    from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
        _attempt_records,
    )

    port_rungs = _attempt_records(
        {
            "attempts": [
                {"tier": "local", "backend_id": "a", "model_id": "m", "duration_ms": 0},
                {
                    "tier": "local",
                    "backend_id": "a",
                    "model_id": "n",
                    "duration_ms": 31,
                },
            ]
        }
    )
    assert [rung.duration_ms for rung in port_rungs] == [0, 31]

    bus_rungs = _attempt_records(
        {"escalation_history": _deployed_terminal()["escalation_history"]}
    )
    assert [rung.duration_ms for rung in bus_rungs] == [2140, 910]
