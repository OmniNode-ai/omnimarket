# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18214: ``_envelope_id`` is a residual gap in
``RUNNER_INJECTED_KEYS``, not a re-occurrence of the OMN-17228 defect.

OBSERVED live on onex-dev (dev-system cluster EC2 ``i-06169517a92b45f86``),
staging run 34687273545, 2026-09-12 ~10:05Z. Pod
``omnimarket-projection-delegation-writer-54f97ff998-h7pq4`` (omnimarket
0.4.65) consumed a ``quality-gate-result.v1`` event and rejected it: pydantic
``ValidationError`` on ``ModelQualityGateResult``, field ``_envelope_id``,
"Extra inputs are not permitted" (``input_value=UUID(...)``) -- routed to
``onex.dlq.omnimarket.projection-delegation-malformed.v1``. Ledger row
2026-09-12T10:35:35Z (``docs/tracking/ROLLING_WORK_LEDGER.md``,
lane=staging-deploy-repair) is the read-only capture of the exact error.

WHY THIS IS THE SAME SEAM AS OMN-17228, NOT THE OMN-16249 ONE. Two distinct
injected-key mechanisms exist in this repo:

* ``omnimarket.projection.handler_shim.RUNTIME_INJECTED_KEYS`` -- the
  omnibase_infra runtime auto-wiring's ``handle(input_data)`` seam
  (OMN-16249). It ALREADY includes ``_envelope_id``.
* ``omnimarket.projection.envelope.RUNNER_INJECTED_KEYS`` -- this writer's
  OWN ``unwrap_envelope``/``strip_runner_injected_keys`` seam (OMN-17228),
  used by ``DelegationProjectionRunner`` (a standalone Kafka consumer that
  parses raw message bytes itself, not a ``handle()`` shim dispatched by the
  runtime). It did NOT include ``_envelope_id`` -- only
  ``{"_envelope", "_event_type", "_correlation_id"}``.

``DelegationProjectionRunner.project_event`` is on the second path (see
``handler_delegation.py::_project_quality_gate_result``), so the fix is the
same allowlist OMN-17228 widened, not the OMN-16249 one -- which is already
correct and untouched here.

Fixture shape: the producer stamps ``_envelope_id`` (a UUID) directly inside
the wire ``payload`` object, the same way OMN-17228's DLQ'd record carried
``_envelope`` -- ``unwrap_envelope``'s payload branch does
``result = dict(raw["payload"])``, so any key already present in the
producer's payload object survives into the dict handed to
``ModelQualityGateResult(**strip_runner_injected_keys(data))`` untouched.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.envelope import (
    RUNNER_INJECTED_KEYS,
    strip_runner_injected_keys,
    unwrap_envelope,
)
from omnimarket.projection.handler_shim import RUNTIME_INJECTED_KEYS
from omnimarket.projection.runner import MessageMeta

_ENVELOPE_TIMESTAMP = datetime(2026, 9, 12, 10, 5, 0, tzinfo=UTC)
_TENANT = "beta-business-proof"


def _wire_record(payload: dict[str, Any]) -> bytes:
    envelope: dict[str, Any] = {
        "payload": payload,
        "envelope_id": str(uuid4()),
        "correlation_id": payload["correlation_id"],
        "event_type": "omnibase-infra.quality-gate-result",
        "envelope_timestamp": _ENVELOPE_TIMESTAMP.isoformat(),
        "tenant_id": _TENANT,
    }
    return json.dumps(envelope).encode("utf-8")


def _quality_gate_delivery_with_envelope_id(*, correlation_id: str) -> dict[str, Any]:
    """The DLQ'd shape: the producer's payload object itself carries
    ``_envelope_id`` (a UUID string), exactly the extra field the live
    ``ValidationError`` named."""
    payload = {
        "correlation_id": correlation_id,
        "passed": True,
        "fail_category": "pass",
        "quality_score": 1.0,
        "failure_reasons": [],
        "fallback_recommended": False,
        "score_source": "deterministic_acceptance",
        "actual_score": 1.0,
        "_envelope_id": str(uuid4()),
    }
    unwrapped = unwrap_envelope(_wire_record(payload))
    assert unwrapped is not None
    assert "_envelope_id" in unwrapped, (
        "fixture setup error: _envelope_id must survive unwrap_envelope's "
        "payload-copy branch for this to replay the live DLQ shape"
    )
    return unwrapped


def _mock_db() -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    db.fetchval = AsyncMock(return_value=None)
    return db


def _run_verdict(mock_db: AsyncMock, *, correlation_id: str, offset: int) -> bool:
    published: list[str] = []

    async def capture(topic: str, value: bytes) -> None:
        published.append(topic)

    runner = DelegationProjectionRunner(publish_fn=capture)
    runner._db = mock_db
    result = asyncio.run(
        runner.project_event(
            runner._topic_quality_gate_result,
            _quality_gate_delivery_with_envelope_id(correlation_id=correlation_id),
            MessageMeta(partition=0, offset=offset, fallback_id=correlation_id),
        )
    )
    return bool(result)


class TestEnvelopeIdIsStrippedNotForbidden:
    def test_strip_runner_injected_keys_removes_envelope_id(self) -> None:
        """Unit-level RED: before the fix, ``_envelope_id`` survives the
        strip and would still blow up model construction."""
        stripped = strip_runner_injected_keys(
            {"correlation_id": "c1", "_envelope_id": str(uuid4()), "_envelope": {}}
        )
        assert "_envelope_id" not in stripped, (
            "_envelope_id must be a member of RUNNER_INJECTED_KEYS -- it is "
            "the residual gap OMN-18214 closes in the allowlist OMN-17228 "
            "widened for _envelope"
        )
        assert "correlation_id" in stripped

    def test_the_writer_does_not_dlq_the_live_shaped_event(self) -> None:
        """RED before OMN-18214: this is the exact shape that DLQ'd on
        onex-dev staging run 34687273545 -- ``ModelQualityGateResult``
        raised ``ValidationError`` on the extra ``_envelope_id`` field and
        the event was routed to
        ``onex.dlq.omnimarket.projection-delegation-malformed.v1`` instead
        of the ``delegation_events`` row being written."""
        mock_db = _mock_db()
        correlation_id = str(uuid4())

        result = _run_verdict(mock_db, correlation_id=correlation_id, offset=1)

        assert result is True

        dlq_calls = [
            call
            for call in mock_db.execute.await_args_list
            if "malformed" in str(call.args[0]).lower()
            or "dlq" in str(call.args[0]).lower()
        ]
        insert_calls = [
            call
            for call in mock_db.execute.await_args_list
            if str(call.args[0]).strip().startswith("INSERT INTO delegation_events")
        ]
        assert not dlq_calls, (
            "the verdict must not be routed to the malformed-projection DLQ "
            "path -- _envelope_id is a runner-injected key, not a producer "
            "field ModelQualityGateResult should ever be asked to accept"
        )
        assert insert_calls, "expected a delegation_events INSERT to be issued"

    def test_runtime_injected_keys_already_covers_envelope_id(self) -> None:
        """Positive control distinguishing the two seams: the OMN-16249
        ``handler_shim`` allowlist already names ``_envelope_id`` -- this
        ticket's gap is exclusively in the OMN-17228 ``envelope.py``
        allowlist, so widening the wrong one would be a second mechanism,
        not the fix the precedent calls for."""
        assert "_envelope_id" in RUNTIME_INJECTED_KEYS

    def test_runner_injected_keys_names_envelope_id(self) -> None:
        assert "_envelope_id" in RUNNER_INJECTED_KEYS
        # The three keys OMN-17228 already covers must not regress.
        assert {"_envelope", "_event_type", "_correlation_id"} <= RUNNER_INJECTED_KEYS
