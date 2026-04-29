"""Handler for repeatable cost token-usage projection snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
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
PUBLISH_TOPIC_COST_TOKEN_USAGE = PUBLISH_TOPICS[0]


class HandlerProjectionCostTokenUsage:
    """Build a deterministic single-event token-usage snapshot."""

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Return a token-usage snapshot for downstream dashboard consumers."""

        prompt_tokens = _int_value(
            _first_present(input_data, "prompt_tokens", "promptTokens")
        )
        completion_tokens = _int_value(
            _first_present(input_data, "completion_tokens", "completionTokens")
        )
        raw_total_tokens = _first_present(input_data, "total_tokens", "totalTokens")
        total_tokens = (
            prompt_tokens + completion_tokens
            if raw_total_tokens is None
            else _int_value(raw_total_tokens)
        )
        return {
            "snapshot_type": "cost_token_usage",
            "window": _text(_first_present(input_data, "window", default="latest")),
            "snapshot_timestamp_minute": _snapshot_timestamp_minute(input_data),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "source_event_count": 1,
        }


class NodeProjectionCostTokenUsage(HandlerProjectionCostTokenUsage):
    """ONEX entry-point wrapper for HandlerProjectionCostTokenUsage."""


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


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _text(value: Any) -> str:
    return str(value).strip()


def _first_present(payload: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return default


__all__ = [
    "PUBLISH_TOPIC_COST_TOKEN_USAGE",
    "SUBSCRIBE_TOPIC_LLM_CALL_COMPLETED",
    "HandlerProjectionCostTokenUsage",
    "NodeProjectionCostTokenUsage",
]
