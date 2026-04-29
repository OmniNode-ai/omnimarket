"""Handler for repeatable cost-by-repository projection snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

SUBSCRIBE_TOPIC_LLM_CALL_COMPLETED = "onex.evt.omniintelligence.llm-call-completed.v1"
PUBLISH_TOPIC_COST_BY_REPO = "onex.evt.omnimarket.cost-by-repo-snapshot.v1"


class HandlerProjectionCostByRepo:
    """Build a deterministic single-event cost-by-repository snapshot."""

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Return a snapshot-shaped payload for downstream dashboard consumers."""

        repo_name = _text(
            input_data.get("repo_name")
            or input_data.get("repoName")
            or input_data.get("repository")
            or "unknown"
        )
        return {
            "snapshot_type": "cost_by_repo",
            "window": _text(input_data.get("window") or "latest"),
            "snapshot_timestamp_minute": _snapshot_timestamp_minute(input_data),
            "repositories": [
                {
                    "repo_name": repo_name,
                    "estimated_cost_usd": _decimal_text(
                        input_data.get("estimated_cost_usd")
                        or input_data.get("estimatedCostUsd")
                        or input_data.get("total_cost_usd")
                        or input_data.get("totalCostUsd")
                        or "0"
                    ),
                    "total_tokens": _int_value(
                        input_data.get("total_tokens") or input_data.get("totalTokens")
                    ),
                }
            ],
            "source_event_count": 1,
        }


class NodeProjectionCostByRepo(HandlerProjectionCostByRepo):
    """ONEX entry-point wrapper for HandlerProjectionCostByRepo."""


def _snapshot_timestamp_minute(payload: dict[str, Any]) -> str:
    raw = (
        payload.get("snapshot_timestamp_minute")
        or payload.get("snapshotTimestampMinute")
        or payload.get("event_timestamp")
        or payload.get("eventTimestamp")
        or payload.get("timestamp_iso")
        or payload.get("timestamp")
        or payload.get("emitted_at")
    )
    dt = _parse_datetime(raw)
    return dt.astimezone(UTC).replace(second=0, microsecond=0).isoformat()


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif value:
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


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _text(value: Any) -> str:
    return str(value).strip()


__all__ = ["HandlerProjectionCostByRepo", "NodeProjectionCostByRepo"]
