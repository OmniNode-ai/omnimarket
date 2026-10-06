# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alert-channel verdict the projection records, driven through the
producer's own classifier.

Every verdict these tests project is the verdict the real classifier returned
for a real observation, not a hand-built payload asserting what the author
hoped the checker would say. A wire model that drifts from the producer's
vocabulary would DLQ the whole checked topic, which is the same shape of
silence the checker exists to end.

The writer is walked with a recording database double exactly the way the
prod-promotion-gate projection tests walk theirs: the statement and its
bound values are the assertions, not a live database.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_alert_channel_liveness_effect.handlers.classify_channel_probe import (
    classify_channel_probe,
)
from omnimarket.nodes.node_alert_channel_liveness_effect.models import (
    EnumAlertChannelStatus,
    ModelAlertChannelLivenessResult,
    ModelAlertChannelObservation,
)
from omnimarket.nodes.node_projection_alert_channel_liveness.handlers.handler_alert_channel_liveness_writer import (
    AlertChannelLivenessProjectionWriter,
)
from omnimarket.nodes.node_projection_alert_channel_liveness.handlers.handler_projection_alert_channel_liveness import (
    HandlerProjectionAlertChannelLiveness,
)
from omnimarket.nodes.node_projection_alert_channel_liveness.models import (
    ModelAlertChannelLivenessProjectionRequest,
    ModelAlertChannelLivenessProjectionResult,
    ModelAlertChannelLivenessResultWire,
)

pytestmark = pytest.mark.unit

#: The interval every result in this suite is produced under; a measured
#: producer fact, so it must reach the row verbatim.
_INTERVAL = 300


def _observation_for(status: EnumAlertChannelStatus) -> ModelAlertChannelObservation:
    """One read-only observation the real classifier carries to that state."""
    if status is EnumAlertChannelStatus.LIVE:
        return ModelAlertChannelObservation(
            credentials_present=True,
            auth_ok=True,
            channel_ok=True,
            bot_is_member=True,
        )
    if status is EnumAlertChannelStatus.DEAD:
        # Slack answers 200 with ok=false; the classifier records the token.
        return ModelAlertChannelObservation(
            credentials_present=True,
            auth_ok=False,
            auth_error="invalid_auth",
        )
    if status is EnumAlertChannelStatus.NOT_CONFIGURED:
        return ModelAlertChannelObservation(credentials_present=False)
    # Credentials resolved; the probe itself never completed.
    return ModelAlertChannelObservation(
        credentials_present=True, transport_error="dns resolution failed"
    )


def _result_for(
    status: EnumAlertChannelStatus,
) -> tuple[ModelAlertChannelLivenessResult, Any]:
    """Drive the real classifier to one state and wrap its verdict as emitted."""
    verdict = classify_channel_probe(_observation_for(status))
    assert verdict.status is status, (
        f"the classifier must reach {status}; it returned {verdict.status}"
    )
    result = ModelAlertChannelLivenessResult(
        probed=True,
        verdict=verdict,
        probe_interval_seconds=_INTERVAL,
        failure_surfaced=status is not EnumAlertChannelStatus.LIVE,
    )
    return result, verdict


def _fold(
    payload: dict[str, Any], **delivery: Any
) -> ModelAlertChannelLivenessProjectionResult:
    """Fold one wire payload with the pure fold, no coordinates unless named."""
    request = ModelAlertChannelLivenessProjectionRequest.model_validate(
        {**payload, **delivery}
    )
    return HandlerProjectionAlertChannelLiveness().handle(request)


class _RecordingDb:
    """A database double that records every statement and its bound values.

    It walks the write path; it does not type-check the SQL. A string bound
    where a TIMESTAMPTZ or a UUID is declared is the question a double cannot
    answer, and it is the question the migration's own column types answer.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return [
            {
                "correlation_id": args[0],
                "status": args[1],
                "healthy": args[2],
                "reason": args[3],
                "slack_error": args[4],
                "probe_interval_seconds": args[5],
                "failure_surfaced": args[6],
                "checked_at": args[7],
            }
        ]


#: The ten values the writer binds, in the column order of its INSERT.
_COLUMNS = (
    "correlation_id",
    "status",
    "healthy",
    "reason",
    "slack_error",
    "probe_interval_seconds",
    "failure_surfaced",
    "checked_at",
    "source_topic",
    "projected_at",
)


def _bound(db: _RecordingDb) -> dict[str, Any]:
    """The single row the writer bound, as a column->value mapping."""
    assert len(db.calls) == 1, f"expected exactly one statement, got {len(db.calls)}"
    _, args = db.calls[0]
    assert len(args) == len(_COLUMNS)
    return dict(zip(_COLUMNS, args, strict=True))


def _project(
    payload: dict[str, Any], *, offset: int = 0
) -> tuple[_RecordingDb, dict[str, Any]]:
    """Drive one result through the writer and return (db, handler result)."""
    writer = AlertChannelLivenessProjectionWriter()
    db = _RecordingDb()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(writer, "_db", db)
        return _drive(writer, db, payload, offset)


def _drive(
    writer: AlertChannelLivenessProjectionWriter,
    db: _RecordingDb,
    payload: dict[str, Any],
    offset: int,
) -> tuple[_RecordingDb, dict[str, Any]]:
    data = dict(payload)
    data["_topic"] = writer.subscribe_topics[0]
    data["_partition"] = 0
    data["_offset"] = offset
    result = writer.handle(data)
    return db, result


# ---------------------------------------------------------------------------
# The fold: every measured state retains its distinct status and reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status", list(EnumAlertChannelStatus), ids=lambda status: status.value
)
def test_fold_preserves_every_status_and_derives_health(
    status: EnumAlertChannelStatus,
) -> None:
    """All four measured states keep their verdict; only LIVE is healthy."""
    result, verdict = _result_for(status)
    outcome = _fold(result.model_dump(mode="json"))

    assert outcome.skipped is False
    row = outcome.row
    assert row is not None, f"{status} is a measured verdict and must be a row"
    assert row.status == status.value
    assert row.healthy is (status is EnumAlertChannelStatus.LIVE)
    # The reason and the interval are the probe's own facts, verbatim.
    assert row.reason == verdict.reason
    assert row.probe_interval_seconds == _INTERVAL
    assert row.failure_surfaced is (status is not EnumAlertChannelStatus.LIVE)
    # A DEAD verdict carries Slack's error token beside the sentence.
    assert row.slack_error == verdict.slack_error
    if status is EnumAlertChannelStatus.DEAD:
        assert verdict.slack_error == "invalid_auth"


def test_a_field_a_later_producer_adds_does_not_refuse_the_result() -> None:
    """Tolerance in the other direction, so the reader ships consumer-first."""
    result, _ = _result_for(EnumAlertChannelStatus.LIVE)
    payload = result.model_dump(mode="json")
    payload["probe_latency_ms"] = 42
    payload["verdict"]["attempt"] = 1

    db, handle_result = _project(payload)
    assert handle_result["rows_upserted"] == 1
    assert _bound(db)["status"] == "LIVE"


def test_a_probed_result_without_a_verdict_is_malformed() -> None:
    """A completed probe that describes nothing is quarantined, not written.

    Inventing a status would record a measurement nobody made; the wire model
    refuses it, which is the trigger that routes it to the declared DLQ.
    """
    with pytest.raises(ValidationError):
        ModelAlertChannelLivenessResultWire.model_validate(
            {
                "probed": True,
                "probe_interval_seconds": _INTERVAL,
                "failure_surfaced": True,
            }
        )


# ---------------------------------------------------------------------------
# The throttled tick: successfully consumed, and it writes nothing
# ---------------------------------------------------------------------------


def test_throttled_tick_skips_and_writes_nothing() -> None:
    """A probed=false tick is not a verdict: no row, a proven zero.

    Writing one would date-stamp the last measured verdict anew, so a lane
    that throttles would read as a stream of fresh measurements.
    """
    tick = ModelAlertChannelLivenessResult(
        probed=False,
        verdict=None,
        probe_interval_seconds=_INTERVAL,
        failure_surfaced=False,
    )
    payload = tick.model_dump(mode="json")

    outcome = _fold(payload)
    assert outcome.skipped is True
    assert outcome.row is None

    db, handle_result = _project(payload)
    assert db.calls == [], "a throttled tick must not reach the database"
    assert handle_result["rows_upserted"] == 0
    assert handle_result["alert_channel_liveness_rows"] == []


# ---------------------------------------------------------------------------
# The writer: one measured verdict becomes one upserted row
# ---------------------------------------------------------------------------


def test_writer_persists_the_measured_verdict() -> None:
    """The runtime-facing writer binds the probe facts and proves the write."""
    result, verdict = _result_for(EnumAlertChannelStatus.DEAD)
    db, handle_result = _project(result.model_dump(mode="json"))
    assert handle_result["rows_upserted"] == 1
    assert len(handle_result["alert_channel_liveness_rows"]) == 1

    row = _bound(db)
    assert row["status"] == "DEAD"
    assert row["healthy"] is False
    assert row["reason"] == verdict.reason
    assert row["slack_error"] == "invalid_auth"
    assert row["probe_interval_seconds"] == _INTERVAL
    assert row["failure_surfaced"] is True
    assert (
        row["source_topic"]
        == AlertChannelLivenessProjectionWriter().subscribe_topics[0]
    )
    assert isinstance(row["correlation_id"], UUID)
    # No event time on the payload and no envelope: one clock per row.
    assert row["checked_at"] == row["projected_at"]

    statement, _ = db.calls[0]
    assert "ON CONFLICT (correlation_id) DO UPDATE" in statement


def test_the_event_time_on_the_payload_is_preserved_as_checked_at() -> None:
    """When the payload carries its own time, the row keeps it, not the clock."""
    result, _ = _result_for(EnumAlertChannelStatus.PROBE_ERROR)
    payload = result.model_dump(mode="json")
    payload["checked_at"] = "2026-08-23T05:27:00+00:00"

    db, handle_result = _project(payload)
    assert handle_result["rows_upserted"] == 1

    row = _bound(db)
    assert row["checked_at"] == datetime.fromisoformat("2026-08-23T05:27:00+00:00")
    assert row["checked_at"] != row["projected_at"]


def test_redelivery_converges_and_distinct_offsets_remain_distinct() -> None:
    """Two deliveries of the same message converge; a later probe does not.

    The fallback identity is derived from topic/partition/offset, so it is
    stable for a redelivery and distinct for a genuinely different message.
    """
    result, _ = _result_for(EnumAlertChannelStatus.LIVE)
    payload = result.model_dump(mode="json")

    first, _ = _project(payload, offset=7)
    again, _ = _project(payload, offset=7)
    later, _ = _project(payload, offset=8)

    assert _bound(first)["correlation_id"] == _bound(again)["correlation_id"]
    assert _bound(first)["correlation_id"] != _bound(later)["correlation_id"]


def test_the_writer_declares_in_process_runtime_dispatch() -> None:
    """Declared, never inferred from the class name (OMN-16874).

    Undeclared, a runner-shaped class is classified STANDALONE: the shared
    runtime subscribes its topics and dispatches nothing, so the node stores
    nothing while reading healthy on lag and on every watermark.
    """
    assert AlertChannelLivenessProjectionWriter.onex_runtime_inprocess_dispatch is True


def test_handle_reports_the_row_count_key_the_runtime_guard_reads() -> None:
    """A written row must not be scored zero by the runtime write-path guard."""
    result, _ = _result_for(EnumAlertChannelStatus.NOT_CONFIGURED)
    _, handle_result = _project(result.model_dump(mode="json"))
    assert handle_result["rows_upserted"] == 1
    assert len(handle_result["alert_channel_liveness_rows"]) == 1
