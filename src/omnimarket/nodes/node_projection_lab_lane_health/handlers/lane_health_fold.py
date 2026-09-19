"""The pure fold behind the lab lane-health projection (OMN-18769).

Three event shapes in, one row per lane out. Everything in this module is a
pure function over dicts and models: no clock, no broker, no database. That is
what lets the ticket's acceptance criteria be falsified by a unit test instead
of by a live lane that may be mid-recovery.

The fold is per-fact, not per-row. An arriving census event replaces the census
fact and leaves the health and receipt facts exactly as they were, each keeping
its own ``observed_at``. There is deliberately no "latest wins over the whole
row" path: that is the shape that lets one fresh fact make two stale ones look
current.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from omnimarket.nodes.node_projection_lab_lane_health.models.enum_lab_lane import (
    EnumLabLane,
    normalize_lane,
)
from omnimarket.nodes.node_projection_lab_lane_health.models.model_lab_lane_health_row import (
    ModelLabLaneHealthRow,
    ModelLaneCensusFact,
    ModelLaneHealthFact,
    ModelLaneReceiptFact,
)
from omnimarket.nodes.node_projection_lab_lane_health.topics import (
    SUBSCRIBE_TOPICS,
    TOPIC_LAB_PASS_RECEIPT,
    TOPIC_LANE_CENSUS,
    TOPIC_RUNTIME_HEALTH,
)

__all__ = [
    "SUBSCRIBE_TOPICS",
    "TOPIC_LAB_PASS_RECEIPT",
    "TOPIC_LANE_CENSUS",
    "TOPIC_RUNTIME_HEALTH",
    "LaneHealthFoldError",
    "apply_event",
    "census_facts",
    "health_fact",
    "parse_timestamp",
    "receipt_fact",
]


class LaneHealthFoldError(ValueError):
    """A source event this reducer cannot fold.

    Raised rather than swallowed. The contract routes malformed events to a DLQ;
    an event that is dropped in a ``try``/``except`` here would leave a lane
    silently frozen at its last good fact, which is the failure this projection
    exists to make visible.
    """


def parse_timestamp(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        raise LaneHealthFoldError(f"{field} is required and must be a timestamp")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LaneHealthFoldError(
            f"{field} is not an ISO-8601 timestamp: {value!r}"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _str_map(raw: Any) -> tuple[dict[str, str], ...]:
    """Coerce a list of objects to a tuple of flat string maps.

    Values are stringified rather than type-checked: these payloads are display
    detail carried verbatim for a panel, and refusing a census finding because
    one of its fields arrived as an int would lose the whole lane's drift list
    over a formatting difference.
    """
    if not isinstance(raw, list):
        return ()
    out: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            out.append({str(k): str(v) for k, v in item.items()})
    return tuple(out)


def census_facts(payload: dict[str, Any]) -> dict[EnumLabLane, ModelLaneCensusFact]:
    """Split one census-observed event into a fact per IN-SCOPE lab lane.

    A census run covers several lanes at once, so the event fans out. Lanes
    outside :data:`EnumLabLane` -- stability-test, judge, the collaborator lane
    -- produce no fact at all and are not counted into any other lane's
    ``drift_count`` (AC6).
    """
    observed_at = parse_timestamp(
        payload.get("observed_at") or payload.get("emitted_at"), "observed_at"
    )
    host = str(payload.get("host", ""))
    findings = _str_map(payload.get("findings"))

    lanes_checked = payload.get("lanes_checked")
    if not isinstance(lanes_checked, list):
        raise LaneHealthFoldError("lanes_checked is required on a census event")

    facts: dict[EnumLabLane, ModelLaneCensusFact] = {}
    for raw_lane in lanes_checked:
        lane = normalize_lane(str(raw_lane))
        if lane is None:
            continue
        own = tuple(
            item for item in findings if normalize_lane(item.get("lane")) is lane
        )
        facts[lane] = ModelLaneCensusFact(
            observed_at=observed_at,
            drift_count=len(own),
            drift_items=own,
            host=host,
        )
    return facts


def health_fact(
    payload: dict[str, Any],
) -> tuple[EnumLabLane, ModelLaneHealthFact] | None:
    """Fold one runtime-health event, or ``None`` when it names no lab lane.

    ``None`` covers two distinct upstream states and deliberately treats them
    the same way: a runtime that predates the ``lane`` field, and a runtime on a
    lane this projection does not hold. Both mean "cannot key this", and the
    only safe answer to that is to hold no row rather than to attribute the
    event to a lane by proximity.
    """
    lane = normalize_lane(payload.get("lane"))
    if lane is None:
        return None
    return lane, ModelLaneHealthFact(
        observed_at=parse_timestamp(payload.get("timestamp"), "timestamp"),
        aggregate=str(payload.get("status", "")),
        dimensions=_str_map(payload.get("dimensions")),
    )


def receipt_fact(
    payload: dict[str, Any],
) -> tuple[EnumLabLane, ModelLaneReceiptFact] | None:
    """Fold one lab-pass receipt event, or ``None`` when it names no lab lane."""
    lane = normalize_lane(payload.get("lane"))
    if lane is None:
        return None
    checks = payload.get("checks")
    failing: tuple[str, ...] = ()
    if isinstance(checks, list):
        failing = tuple(
            str(check.get("name", ""))
            for check in checks
            if isinstance(check, dict) and not check.get("ok", False)
        )
    return lane, ModelLaneReceiptFact(
        observed_at=parse_timestamp(payload.get("finished_at"), "finished_at"),
        sha=str(payload.get("sha", "")),
        result=str(payload.get("result", "")),
        failing_checks=failing,
    )


def apply_event(
    rows: dict[EnumLabLane, ModelLabLaneHealthRow],
    *,
    topic: str,
    payload: dict[str, Any],
) -> tuple[ModelLabLaneHealthRow, ...]:
    """Fold one source event into ``rows`` in place; return the rows it changed.

    The return value is the publish set: a census event covering two lanes
    republishes two rows, and an event naming no lab lane republishes none. A
    caller that republished every row on every event would put an unchanged
    lane's row back on the bus with a newer ``projected_at``, which is a
    freshness claim nothing observed.
    """
    touched: list[ModelLabLaneHealthRow] = []

    def _upsert(lane: EnumLabLane, **fields: Any) -> None:
        current = rows.get(lane) or ModelLabLaneHealthRow(lane=lane)
        updated = current.model_copy(update=fields)
        rows[lane] = updated
        touched.append(updated)

    if topic == TOPIC_LANE_CENSUS:
        for lane, fact in census_facts(payload).items():
            _upsert(lane, census=fact)
    elif topic == TOPIC_RUNTIME_HEALTH:
        resolved_health = health_fact(payload)
        if resolved_health is not None:
            _upsert(resolved_health[0], health=resolved_health[1])
    elif topic == TOPIC_LAB_PASS_RECEIPT:
        resolved_receipt = receipt_fact(payload)
        if resolved_receipt is not None:
            _upsert(resolved_receipt[0], receipt=resolved_receipt[1])
    else:
        raise LaneHealthFoldError(f"unsubscribed topic {topic!r}")

    return tuple(touched)
