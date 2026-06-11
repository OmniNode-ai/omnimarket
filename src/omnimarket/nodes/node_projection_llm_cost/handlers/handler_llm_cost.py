"""LLM cost projection: Kafka -> llm_call_metrics table (contract-owned writer).

This is the DEPLOYED runtime entrypoint for node_projection_llm_cost
(omnibase_infra/docker/catalog/services/omnimarket-projection-llm-cost.yaml
``command`` runs ``python -m ...handlers.handler_llm_cost``). It consumes
``onex.evt.omniintelligence.llm-call-completed.v1`` and materializes one
per-call row into ``llm_call_metrics`` — the read model the dashboard reads via
node_projection_cost_token_usage (cost.token_usage.v1) and node_ab_compare_reducer
(ab-compare.v1).

OMN-13001: this runner previously wrote ``llm_cost_aggregates`` with a drifted
column set (``bucket_time, granularity, model_name, ...``) that no longer matched
the deployed table (``aggregation_key, "window", total_cost_usd, ...`` from
migration 0001_create_llm_cost_aggregates.sql / infra 031), so it landed nothing.
The aggregate read model is owned by node_projection_cost_summary, not this node.
This runner is now the single contract-owned writer of ``llm_call_metrics``;
the per-call insert logic lives in row_llm_call_metrics.build_llm_call_metrics_row.

The write model column set is asserted equal to the migration column set by the
schema-parity test (tests/test_schema_parity_projection_llm_cost.py) — the
ratchet that prevents this drift class from recurring.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_llm_cost.handlers.row_llm_call_metrics import (
    LLM_CALL_METRICS_COLUMNS,
    build_llm_call_metrics_row,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

logger = logging.getLogger(__name__)

TABLE = "llm_call_metrics"
CONFLICT_KEY = "input_hash"


class LlmCostProjectionRunner(BaseProjectionRunner):
    """Project llm-call-completed events into the llm_call_metrics table.

    Per-call append, deduplicated by ``input_hash`` via
    ``ON CONFLICT (input_hash) DO NOTHING`` (the partial unique index from
    migration 0001_create_llm_call_metrics.sql).
    """

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as f:
            self._contract: dict[str, Any] = yaml.safe_load(f)

        _tables = self._contract.get("db_io", {}).get("db_tables", [])
        _write_tables = [t["name"] for t in _tables if t.get("access") == "write"]
        if TABLE not in _write_tables:
            raise ValueError(
                f"Contract must declare {TABLE!r} as a write model; "
                f"db_io write tables are {_write_tables!r}"
            )

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

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

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        row = build_llm_call_metrics_row(data)
        await self.db.execute(
            f"""
            INSERT INTO {TABLE} (
                correlation_id, session_id, run_id, model_id,
                prompt_tokens, completion_tokens, total_tokens,
                estimated_cost_usd, latency_ms,
                usage_source, usage_is_estimated, usage_raw,
                input_hash, source,
                code_version, contract_version,
                created_at
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7,
                $8, $9,
                $10::usage_source_type, $11, $12::jsonb,
                $13, $14,
                $15, $16,
                $17
            )
            ON CONFLICT ({CONFLICT_KEY}) DO NOTHING
            """,
            row["correlation_id"],
            row["session_id"],
            row["run_id"],
            row["model_id"],
            row["prompt_tokens"],
            row["completion_tokens"],
            row["total_tokens"],
            row["estimated_cost_usd"],
            row["latency_ms"],
            row["usage_source"],
            row["usage_is_estimated"],
            row["usage_raw"],
            row["input_hash"],
            row["source"],
            row["code_version"],
            row["contract_version"],
            row["created_at"],
        )
        return True


__all__ = [
    "CONFLICT_KEY",
    "LLM_CALL_METRICS_COLUMNS",
    "TABLE",
    "LlmCostProjectionRunner",
    "build_llm_call_metrics_row",
]


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = LlmCostProjectionRunner()
    asyncio.run(runner.run())
