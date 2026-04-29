"""Handler for repeatable cost token-usage projection snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

SUBSCRIBE_TOPIC_LLM_CALL_COMPLETED = "onex.evt.omniintelligence.llm-call-completed.v1"
PUBLISH_TOPIC_COST_TOKEN_USAGE = "onex.evt.omnimarket.cost-token-usage-snapshot.v1"


class HandlerProjectionCostTokenUsage:
    """Build a deterministic single-event token-usage snapshot."""

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Return a token-usage snapshot for downstream dashboard consumers."""

        prompt_tokens = _int_value(
            input_data.get("prompt_tokens") or input_data.get("promptTokens")
        )
        completion_tokens = _int_value(
            input_data.get("completion_tokens") or input_data.get("completionTokens")
        )
        total_tokens = _int_value(
            input_data.get("total_tokens") or input_data.get("totalTokens")
        )
        if total_tokens == 0:
            total_tokens = prompt_tokens + completion_tokens
        return {
            "snapshot_type": "cost_token_usage",
            "window": _text(input_data.get("window") or "latest"),
            "snapshot_timestamp_minute": _snapshot_timestamp_minute(input_data),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "source_event_count": 1,
        }


class NodeProjectionCostTokenUsage(HandlerProjectionCostTokenUsage):
    """ONEX entry-point wrapper for HandlerProjectionCostTokenUsage."""


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


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _text(value: Any) -> str:
    return str(value).strip()


__all__ = ["HandlerProjectionCostTokenUsage", "NodeProjectionCostTokenUsage"]
