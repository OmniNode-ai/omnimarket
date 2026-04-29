"""Handler for repeatable cost summary projection snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
SUBSCRIBE_TOPICS = contract_subscribe_topics(_CONTRACT_PATH)
PUBLISH_TOPICS = contract_publish_topics(_CONTRACT_PATH)
SUBSCRIBE_TOPIC_LLM_CALL_COMPLETED = SUBSCRIBE_TOPICS[0]
SUBSCRIBE_TOPIC_SAVINGS_ESTIMATED = SUBSCRIBE_TOPICS[1]
PUBLISH_TOPIC_COST_SUMMARY = PUBLISH_TOPICS[0]


class HandlerProjectionCostSummary:
    """Build a deterministic single-event cost summary snapshot."""

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Return a summary snapshot payload for downstream dashboard consumers."""

        estimated_cost = _decimal_text(
            _first_present(
                input_data,
                "estimated_cost_usd",
                "estimatedCostUsd",
                "total_cost_usd",
                "totalCostUsd",
                "cloud_cost_usd",
                "cloudCostUsd",
            )
        )
        savings = _decimal_text(_first_present(input_data, "savings_usd", "savingsUsd"))
        return {
            "snapshot_type": "cost_summary",
            "window": _text(_first_present(input_data, "window", default="latest")),
            "snapshot_timestamp_minute": _snapshot_timestamp_minute(input_data),
            "total_estimated_cost_usd": estimated_cost,
            "total_savings_usd": savings,
            "source_event_count": 1,
        }


class NodeProjectionCostSummary(HandlerProjectionCostSummary):
    """ONEX entry-point wrapper for HandlerProjectionCostSummary."""


def _snapshot_timestamp_minute(payload: dict[str, Any]) -> str:
    raw = _first_present(
        payload,
        "snapshot_timestamp_minute",
        "snapshotTimestampMinute",
        "event_timestamp",
        "eventTimestamp",
        "timestamp_iso",
        "timestamp",
        "emitted_at",
    )
    dt = _parse_datetime(raw)
    return dt.astimezone(UTC).replace(second=0, microsecond=0).isoformat()


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif value is not None:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.now(tz=UTC)
    else:
        dt = datetime.now(tz=UTC)
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _decimal_text(value: Any) -> str:
    try:
        return str(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return "0"


def _text(value: Any) -> str:
    return str(value).strip()


def _first_present(payload: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return default


__all__ = [
    "PUBLISH_TOPIC_COST_SUMMARY",
    "SUBSCRIBE_TOPIC_LLM_CALL_COMPLETED",
    "SUBSCRIBE_TOPIC_SAVINGS_ESTIMATED",
    "HandlerProjectionCostSummary",
    "NodeProjectionCostSummary",
]
