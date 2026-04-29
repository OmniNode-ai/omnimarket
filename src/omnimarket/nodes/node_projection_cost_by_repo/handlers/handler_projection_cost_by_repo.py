"""Handler for repeatable cost-by-repository projection snapshots."""

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
PUBLISH_TOPIC_COST_BY_REPO = PUBLISH_TOPICS[0]


class HandlerProjectionCostByRepo:
    """Build a deterministic single-event cost-by-repository snapshot."""

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Return a snapshot-shaped payload for downstream dashboard consumers."""

        repo_name = _text(
            _first_present(
                input_data, "repo_name", "repoName", "repository", default="unknown"
            )
        )
        return {
            "snapshot_type": "cost_by_repo",
            "window": _text(_first_present(input_data, "window", default="latest")),
            "snapshot_timestamp_minute": _snapshot_timestamp_minute(input_data),
            "repositories": [
                {
                    "repo_name": repo_name,
                    "estimated_cost_usd": _decimal_text(
                        _first_present(
                            input_data,
                            "estimated_cost_usd",
                            "estimatedCostUsd",
                            "total_cost_usd",
                            "totalCostUsd",
                        )
                    ),
                    "total_tokens": _int_value(
                        _first_present(input_data, "total_tokens", "totalTokens")
                    ),
                }
            ],
            "source_event_count": 1,
        }


class NodeProjectionCostByRepo(HandlerProjectionCostByRepo):
    """ONEX entry-point wrapper for HandlerProjectionCostByRepo."""


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
    "PUBLISH_TOPIC_COST_BY_REPO",
    "SUBSCRIBE_TOPIC_LLM_CALL_COMPLETED",
    "HandlerProjectionCostByRepo",
    "NodeProjectionCostByRepo",
]
