"""Routing decision projection: Kafka -> agent_routing_decisions table."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    safe_parse_date,
)

logger = logging.getLogger(__name__)

KNOWN_PROJECTION_TABLES: frozenset[str] = frozenset(
    {
        "agent_routing_decisions",
        "delegation_events",
        "delegation_shadow_comparisons",
        "llm_cost_aggregates",
        "node_service_registry",
        "baselines_snapshots",
        "baselines_comparisons",
        "baselines_trend",
        "baselines_breakdown",
        "savings_estimates",
        "session_outcomes",
        "injection_effectiveness",
    }
)


class RoutingDecisionProjectionRunner(BaseProjectionRunner):
    """Projects routing-decision events into agent_routing_decisions table.

    SQL: INSERT ... ON CONFLICT (id) DO NOTHING (append-only observability).
    Consumes onex.evt.omniclaude.routing-decision.v1 emitted by the
    omniclaude polymorphic router (HandlerRoutingEmitter payload shape).
    """

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as f:
            self._contract: dict[str, Any] = yaml.safe_load(f)

        _tables = self._contract.get("db_io", {}).get("db_tables", [])
        _by_role = {t["role"]: t["name"] for t in _tables}

        for role, name in _by_role.items():
            if name not in KNOWN_PROJECTION_TABLES:
                raise ValueError(
                    f"Unknown table role {role!r} maps to {name!r} which is not in KNOWN_PROJECTION_TABLES"
                )

        if "routing_decisions" not in _by_role:
            raise ValueError("Contract missing required table role 'routing_decisions'")

        self._table_routing: str = _by_role["routing_decisions"]

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Delegates to project_event via asyncio.run().
        """
        topics = self.subscribe_topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
        )
        ok = asyncio.run(self.project_event(topic, input_data, meta))
        return {"projected": ok}

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        decision_id = (
            data.get("id")
            or data.get("decision_id")
            or data.get("decisionId")
            or meta.fallback_id
            or str(uuid4())
        )
        correlation_id = data.get("correlation_id") or data.get("correlationId") or None
        selected_agent = data.get("selected_agent") or data.get("selectedAgent") or ""
        confidence_score = data.get("confidence_score") or data.get("confidenceScore")
        created_at = safe_parse_date(
            data.get("created_at")
            or data.get("createdAt")
            or data.get("emitted_at")
            or data.get("emittedAt")
            or data.get("timestamp")
        )
        request_type = data.get("request_type") or data.get("requestType") or None
        alternatives = data.get("alternatives")
        routing_reason = data.get("routing_reason") or data.get("routingReason") or None
        domain = data.get("domain") or None
        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        project_path = data.get("project_path") or data.get("projectPath") or None
        project_name = data.get("project_name") or data.get("projectName") or None
        claude_session_id = (
            data.get("claude_session_id")
            or data.get("claudeSessionId")
            or data.get("session_id")
            or data.get("sessionId")
            or None
        )

        await self.db.execute(
            f"""
            INSERT INTO {self._table_routing} (
              id, correlation_id, selected_agent, confidence_score,
              created_at, request_type, alternatives, routing_reason,
              domain, metadata, project_path, project_name, claude_session_id
            ) VALUES (
              $1, $2, $3, $4,
              $5, $6, $7::jsonb, $8,
              $9, $10::jsonb, $11, $12, $13
            )
            ON CONFLICT (id) DO NOTHING
            """,
            str(decision_id),
            str(correlation_id) if correlation_id else None,
            str(selected_agent),
            confidence_score,
            created_at,
            str(request_type) if request_type else None,
            json.dumps(alternatives) if alternatives is not None else None,
            str(routing_reason) if routing_reason else None,
            str(domain) if domain else None,
            json.dumps(metadata),
            str(project_path) if project_path else None,
            str(project_name) if project_name else None,
            str(claude_session_id) if claude_session_id else None,
        )
        return True


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = RoutingDecisionProjectionRunner()
    asyncio.run(runner.run())
