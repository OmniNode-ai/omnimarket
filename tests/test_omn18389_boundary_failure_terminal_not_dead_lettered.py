# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18389: a consume-boundary failure terminal is an answer to a caller, not
a malformed delegation, so the projection must not dead-letter it.

The runtime's auto-wiring boundary publishes a ``ModelBoundaryFailureTerminal``
onto a contract's own failure terminal topic when a command is refused before
any handler runs (here: ``delegate-skill.v1`` refused for a ``task_type`` outside
the accepted set). That topic is ``delegate-skill-failed.v1``, which this node
projects. The terminal carries ``status`` / ``failure_code`` / ``failure_reason``
and nothing of a delegation (no ``task_type``, no tenant, no attempts), so
``ModelDelegateSkillTerminalProjection`` rejects it and the record lands on
``projection-delegation-malformed.v1`` -- observed on the dev lane as 45
arrivals between 2026-10-18T03:42Z and 2026-10-19T03:46Z with
``task_type Field required``.

No field on the terminal model is relaxed by the fix: a delegation terminal
still has to carry ``task_type``. The runner recognises the other payload by
the ``payload_type`` the emitter stamps on the wire envelope (the same key the
runtime's own cascade guard reads) and does not treat it as a delegation.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.runner import MessageMeta

pytestmark = [pytest.mark.unit]

_BOUNDARY_PAYLOAD_TYPE = "ModelBoundaryFailureTerminal"


async def _publish(topic: str, value: bytes) -> None:
    return


def _delivery(correlation_id: str, payload_type: str | None) -> dict[str, Any]:
    """The boundary terminal in the runner's wire shape: payload unwrapped,
    whole envelope under ``_envelope`` (the record read from the sink)."""
    payload = {
        "correlation_id": correlation_id,
        "status": "failed",
        "failure_class": "ValueError",
        "failure_code": "ONEX_CORE_345_ENVELOPE_VALIDATION_FAILED",
        "retryable": False,
        "failure_reason": "task_type: Input should be 'test', 'document' or 'research'",
        "origin_topic": "onex.cmd.omnimarket.delegate-skill.v1",
        "terminalized_at": "2026-10-19T03:46:46.332820Z",
    }
    envelope: dict[str, Any] = {
        "payload": payload,
        "envelope_id": str(uuid4()),
        "correlation_id": correlation_id,
        "source_tool": "auto-wiring-boundary",
        "target_tool": "onex.evt.omnimarket.delegate-skill-failed.v1",
        "event_type": "omnimarket.delegate-skill-failed",
        "payload_type": payload_type,
    }
    unwrapped = unwrap_envelope(json.dumps(envelope).encode("utf-8"))
    assert unwrapped is not None
    return unwrapped


def _runner(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[DelegationProjectionRunner, AsyncMock, AsyncMock]:
    """The runner with its database and malformed-sink route replaced by mocks,
    returned beside them so the assertions read the mocks directly."""
    runner = DelegationProjectionRunner(publish_fn=_publish)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    dlq = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_db", db)
    monkeypatch.setattr(runner, "_route_malformed_to_dlq", dlq)
    return runner, dlq, db


class TestBoundaryFailureTerminalIsNotMalformed:
    @pytest.mark.asyncio
    async def test_the_boundary_terminal_is_not_dead_lettered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, dlq, db = _runner(monkeypatch)
        correlation_id = str(uuid4())

        ok = await runner.project_event(
            runner._topic_delegate_skill_failed,
            _delivery(correlation_id, _BOUNDARY_PAYLOAD_TYPE),
            MessageMeta(partition=0, offset=34275, fallback_id=correlation_id),
        )

        assert ok is True, "the offset must advance over a boundary terminal"
        assert dlq.await_count == 0, (
            "a boundary failure terminal was routed to the malformed sink -- "
            "the delegate-skill-failed topic is again dead-lettering the "
            "runtime's own answer to a refused command"
        )
        assert db.execute.await_count == 0, (
            "a boundary failure terminal produced a database write; it is not "
            "a delegation and carries no tenant to write under"
        )

    @pytest.mark.asyncio
    async def test_a_delegation_terminal_missing_task_type_is_still_dead_lettered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control: the same payload WITHOUT the boundary payload_type
        is a genuinely malformed delegation terminal and must still reach the
        sink. The fix keys on the emitter's stamp, never on payload shape."""
        runner, dlq, _db = _runner(monkeypatch)
        correlation_id = str(uuid4())

        await runner.project_event(
            runner._topic_delegate_skill_failed,
            _delivery(correlation_id, None),
            MessageMeta(partition=0, offset=1, fallback_id=correlation_id),
        )

        assert dlq.await_count == 1
        assert dlq.await_args is not None
        reason = dlq.await_args.args[1]
        assert "task_type" in reason

    @pytest.mark.asyncio
    async def test_a_different_payload_type_is_still_dead_lettered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, dlq, _db = _runner(monkeypatch)
        correlation_id = str(uuid4())

        await runner.project_event(
            runner._topic_delegate_skill_failed,
            _delivery(correlation_id, "ModelSomethingElse"),
            MessageMeta(partition=0, offset=2, fallback_id=correlation_id),
        )

        assert dlq.await_count == 1
