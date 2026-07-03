"""Handler for repeatable skill-executions projection snapshots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.nodes.node_projection_skill_executions.handlers.row_skill_executions import (
    build_skill_executions_row,
    compute_receipt_coverage,
)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
SUBSCRIBE_TOPICS = contract_subscribe_topics(_CONTRACT_PATH)
PUBLISH_TOPICS = contract_publish_topics(_CONTRACT_PATH)
SUBSCRIBE_TOPIC_SKILL_STARTED = SUBSCRIBE_TOPICS[0]
SUBSCRIBE_TOPIC_SKILL_COMPLETED = (
    SUBSCRIBE_TOPICS[1] if len(SUBSCRIBE_TOPICS) > 1 else SUBSCRIBE_TOPICS[0]
)
PUBLISH_TOPIC_SKILL_EXECUTIONS = PUBLISH_TOPICS[0]


class HandlerProjectionSkillExecutions:
    """Build a deterministic single-event skill-executions snapshot payload."""

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Return a snapshot-shaped payload for downstream dashboard consumers."""
        topic = str(input_data.get("_topic", ""))
        row = build_skill_executions_row(input_data, topic)
        return {
            "snapshot_type": "skill_executions",
            "skill_name": row["skill_name"],
            "repo_id": row["repo_id"],
            "window": row["window"],
            "snapshot_timestamp_minute": row["snapshot_timestamp_minute"].isoformat(),
            "started_count": row["started_count"],
            "completed_count": row["completed_count"],
            "success_count": row["success_count"],
            "failed_count": row["failed_count"],
            "partial_count": row["partial_count"],
            "receipt_coverage": compute_receipt_coverage(
                row["started_count"], row["completed_count"]
            ),
            "source_event_count": 1,
        }


class NodeProjectionSkillExecutions(HandlerProjectionSkillExecutions):
    """ONEX entry-point wrapper for HandlerProjectionSkillExecutions."""


__all__ = [
    "PUBLISH_TOPIC_SKILL_EXECUTIONS",
    "SUBSCRIBE_TOPIC_SKILL_COMPLETED",
    "SUBSCRIBE_TOPIC_SKILL_STARTED",
    "HandlerProjectionSkillExecutions",
    "NodeProjectionSkillExecutions",
]
