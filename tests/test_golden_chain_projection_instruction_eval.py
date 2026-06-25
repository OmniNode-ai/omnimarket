# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_projection_instruction_eval.

OMN-12998: this node materialises instruction-eval-result events into the
instruction_eval_aggregate_snapshots table — the read model the omnidash
InstructionEvalHeatmap panel reads via the canonical projection API at
onex.evt.omnimarket.instruction-eval-aggregate-snapshot.v1.
"""

from __future__ import annotations

import asyncio

import yaml

from omnimarket.nodes.node_projection_instruction_eval.handlers.handler_instruction_eval import (
    InstructionEvalProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

TABLE = "instruction_eval_aggregate_snapshots"
INPUT_TOPIC = "onex.evt.omnimarket.instruction-eval-result.v1"

CONTRACT_PATH = "src/omnimarket/nodes/node_projection_instruction_eval/contract.yaml"


class TestInstructionEvalProjectionGoldenChain:
    """Golden chain: event in -> snapshot row out.

    Uses a recording DB (no real Postgres) so the chain verifies the
    correct SQL shape, bound parameters, and conflict-update path without
    requiring infrastructure.
    """

    def _recording_runner(
        self,
    ) -> tuple[InstructionEvalProjectionRunner, list[tuple[object, ...]]]:
        captured: list[tuple[object, ...]] = []

        class _RecordingDB:
            async def execute(self, *args: object, **kwargs: object) -> None:
                captured.append(args)

        runner = InstructionEvalProjectionRunner()
        runner._db = _RecordingDB()  # type: ignore[assignment]
        return runner, captured

    def test_single_event_produces_one_upsert(self) -> None:
        runner, captured = self._recording_runner()
        meta = MessageMeta(partition=0, offset=1, fallback_id="gc-001")

        ok = asyncio.run(
            runner.project_event(
                INPUT_TOPIC,
                {
                    "model": "ds4-flash",
                    "task": "python-version",
                    "context_mode": "chunk",
                    "pass_rate": 1.0,
                    "output_tokens": 314,
                    "runs": 5,
                },
                meta,
            )
        )

        assert ok is True
        assert len(captured) == 1
        sql = str(captured[0][0])
        assert f"INSERT INTO {TABLE}" in sql
        assert "ON CONFLICT" in sql

    def test_golden_chain_all_context_modes(self) -> None:
        """All three context modes produce a valid upsert."""
        for context_mode in ("baseline", "chunk", "full-claude-md"):
            runner, captured = self._recording_runner()
            meta = MessageMeta(partition=0, offset=0, fallback_id=f"gc-{context_mode}")
            ok = asyncio.run(
                runner.project_event(
                    INPUT_TOPIC,
                    {
                        "model": "qwen-27b",
                        "task": "git-commit-style",
                        "context_mode": context_mode,
                        "pass_rate": 0.6667,
                        "output_tokens": 822,
                        "runs": 5,
                    },
                    meta,
                )
            )
            assert ok is True, f"project_event failed for context_mode={context_mode!r}"
            assert len(captured) == 1

    def test_golden_chain_absent_pass_rate_passes_null(self) -> None:
        """Missing pass_rate is passed as None (NULL) not 0."""
        runner, captured = self._recording_runner()
        meta = MessageMeta(partition=0, offset=2, fallback_id="gc-null-pr")

        asyncio.run(
            runner.project_event(
                INPUT_TOPIC,
                {
                    "model": "qwen-35b",
                    "task": "strongly-typed-models",
                    "context_mode": "baseline",
                    # no pass_rate field
                    "output_tokens": 3006,
                    "runs": 5,
                },
                meta,
            )
        )

        params = captured[0][1:]
        # pass_rate should be None (NULL in DB), not 0.0
        assert None in params, (
            "absent pass_rate must be passed as None (DB NULL), never 0.0"
        )

    def test_golden_chain_bound_params_correctness(self) -> None:
        """Bound parameters carry the exact values from the event."""
        runner, captured = self._recording_runner()
        meta = MessageMeta(partition=0, offset=3, fallback_id="gc-params")

        asyncio.run(
            runner.project_event(
                INPUT_TOPIC,
                {
                    "model": "ds4-flash",
                    "task": "no-hardcoded-paths",
                    "context_mode": "full-claude-md",
                    "pass_rate": 0.4666,
                    "output_tokens": 557,
                    "runs": 5,
                },
                meta,
            )
        )

        params = captured[0][1:]
        assert "ds4-flash" in params
        assert "no-hardcoded-paths" in params
        assert "full-claude-md" in params
        assert 557 in params
        assert 5 in params

    def test_event_bus_wiring(self) -> None:
        """Contract subscribes to the canonical instruction-eval-result topic."""
        with open(CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        assert INPUT_TOPIC in contract["event_bus"]["subscribe_topics"]

    def test_projection_api_topic_declared(self) -> None:
        """Contract projection_api exposes the canonical snapshot topic."""
        with open(CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        projection_topic = "onex.evt.omnimarket.instruction-eval-aggregate-snapshot.v1"
        assert contract["projection_api"]["topic"] == projection_topic

    def test_contract_declares_write_table(self) -> None:
        """Contract db_io declares instruction_eval_aggregate_snapshots as write."""
        with open(CONTRACT_PATH) as f:
            contract = yaml.safe_load(f)
        write_tables = [
            t["name"]
            for t in contract["db_io"]["db_tables"]
            if t.get("access") == "write"
        ]
        assert TABLE in write_tables
