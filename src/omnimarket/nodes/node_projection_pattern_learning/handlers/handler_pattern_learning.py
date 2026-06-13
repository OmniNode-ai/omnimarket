"""Pattern learning projection: Kafka -> pattern_learning_artifacts table.

This runner is the consume-leg the pattern_learning golden chain was missing:
it subscribes to onex.evt.omniintelligence.pattern-stored.v1 and UPSERTs each
event into pattern_learning_artifacts, keyed on pattern_id (OMN-13124).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import yaml

from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    safe_float,
    safe_parse_date,
)

logger = logging.getLogger(__name__)

KNOWN_PROJECTION_TABLES: frozenset[str] = frozenset(
    {
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
        "pattern_learning_artifacts",
    }
)


class PatternLearningProjectionRunner(BaseProjectionRunner):
    """Projects pattern-stored events into pattern_learning_artifacts table.

    SQL: INSERT ... ON CONFLICT (pattern_id) DO UPDATE.
    UPSERT key: pattern_id (latest-state-wins).
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

        if "artifacts" not in _by_role:
            raise ValueError("Contract missing required table role 'artifacts'")

        self._table_artifacts: str = _by_role["artifacts"]

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
        pattern_id = (
            data.get("pattern_id") or data.get("patternId") or data.get("id") or ""
        )
        if not pattern_id:
            keys = sorted(k for k in data if not k.startswith("_"))
            logger.warning(
                "pattern-stored event missing pattern_id -- skipping. Keys: %s",
                keys,
            )
            return True

        pattern_name = data.get("pattern_name") or data.get("patternName") or ""
        pattern_type = (
            data.get("pattern_type")
            or data.get("patternType")
            or data.get("domain")
            or ""
        )
        language = data.get("language") or None
        lifecycle_state = (
            data.get("lifecycle_state")
            or data.get("lifecycleState")
            or data.get("state")
            or "candidate"
        )
        composite_score = safe_float(
            data.get("composite_score")
            or data.get("compositeScore")
            or data.get("confidence")
        )
        correlation_id = (
            data.get("correlation_id")
            or data.get("correlationId")
            or meta.fallback_id
            or None
        )
        state_changed_at = safe_parse_date(
            data.get("state_changed_at")
            or data.get("stateChangedAt")
            or data.get("stored_at")
            or data.get("storedAt")
        )

        scoring_evidence = _coerce_mapping(
            data.get("scoring_evidence") or data.get("scoringEvidence")
        )
        signature = _coerce_signature(data.get("signature"))
        metrics = _coerce_mapping(data.get("metrics"))
        metadata = _coerce_mapping(data.get("metadata"))

        await self.db.execute(
            f"""
            INSERT INTO {self._table_artifacts} (
              pattern_id, pattern_name, pattern_type, language, lifecycle_state,
              state_changed_at, composite_score, scoring_evidence, signature,
              metrics, metadata, correlation_id, updated_at, projected_at
            ) VALUES (
              $1, $2, $3, $4, $5,
              $6, $7, $8::jsonb, $9::jsonb,
              $10::jsonb, $11::jsonb, $12, NOW(), NOW()
            )
            ON CONFLICT (pattern_id) DO UPDATE SET
              pattern_name = EXCLUDED.pattern_name,
              pattern_type = EXCLUDED.pattern_type,
              language = EXCLUDED.language,
              lifecycle_state = EXCLUDED.lifecycle_state,
              state_changed_at = EXCLUDED.state_changed_at,
              composite_score = EXCLUDED.composite_score,
              scoring_evidence = EXCLUDED.scoring_evidence,
              signature = EXCLUDED.signature,
              metrics = EXCLUDED.metrics,
              metadata = EXCLUDED.metadata,
              correlation_id = EXCLUDED.correlation_id,
              updated_at = NOW(),
              projected_at = NOW()
            """,
            str(pattern_id),
            str(pattern_name),
            str(pattern_type),
            str(language) if language else None,
            str(lifecycle_state),
            state_changed_at,
            composite_score,
            json.dumps(scoring_evidence),
            json.dumps(signature),
            json.dumps(metrics),
            json.dumps(metadata),
            str(correlation_id) if correlation_id else None,
        )
        return True


def _coerce_mapping(value: Any) -> dict[str, Any]:
    """Return value as a JSON-able mapping, defaulting to an empty object."""
    if isinstance(value, dict):
        return value
    return {}


def _coerce_signature(value: Any) -> dict[str, Any]:
    """Normalize a signature into a JSON object.

    The omniintelligence pattern-stored event carries signature as a string;
    the projection column is JSONB, so wrap a scalar signature in an object.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        return {"value": value}
    return {}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = PatternLearningProjectionRunner()
    asyncio.run(runner.run())
