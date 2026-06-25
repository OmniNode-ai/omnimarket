# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Instruction-eval projection: Kafka -> instruction_eval_aggregate_snapshots.

OMN-12998: This is the deployed runtime entrypoint for
node_projection_instruction_eval. It consumes
onex.evt.omnimarket.instruction-eval-result.v1 events emitted by the
instruction-eval runner / scorer (onex-self-extending-agent/eval/
instruction-eval lineage) and materialises one per-(model, task, context_mode)
aggregate row into instruction_eval_aggregate_snapshots — the read model the
omnidash InstructionEvalHeatmap panel reads via
onex.evt.omnimarket.instruction-eval-aggregate-snapshot.v1.

Row builder (row_instruction_eval.build_instruction_eval_row) is the single
write authority and column set. On conflict the most-recent values overwrite:
a new eval run for the same (model, task, context_mode) cell replaces the
previous aggregate (last-write-wins within a unique key).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_instruction_eval.handlers.row_instruction_eval import (
    INSTRUCTION_EVAL_CONFLICT_COLUMNS,
    build_instruction_eval_row,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

logger = logging.getLogger(__name__)

TABLE = "instruction_eval_aggregate_snapshots"
CONFLICT_KEY = ", ".join(INSTRUCTION_EVAL_CONFLICT_COLUMNS)


class InstructionEvalProjectionRunner(BaseProjectionRunner):
    """Project instruction-eval-result events into the aggregate snapshots table.

    Per-(model, task, context_mode) last-write-wins: a new eval run for the
    same cell replaces the previous aggregate with the most-recent values.
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
        row = build_instruction_eval_row(data)

        # pass_rate may be None (honest absent-data sentinel). The column is
        # nullable; we pass None directly so asyncpg inserts NULL.
        await self.db.execute(
            f"""
            INSERT INTO {TABLE} (
                model, task, context_mode,
                pass_rate, output_tokens, runs
            ) VALUES (
                $1, $2, $3,
                $4, $5, $6
            )
            ON CONFLICT ({CONFLICT_KEY}) DO UPDATE SET
                pass_rate = EXCLUDED.pass_rate,
                output_tokens = EXCLUDED.output_tokens,
                runs = EXCLUDED.runs,
                updated_at = NOW()
            """,
            row["model"],
            row["task"],
            row["context_mode"],
            row["pass_rate"],
            row["output_tokens"],
            row["runs"],
        )
        return True


__all__ = [
    "CONFLICT_KEY",
    "TABLE",
    "InstructionEvalProjectionRunner",
]


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = InstructionEvalProjectionRunner()
    asyncio.run(runner.run())
