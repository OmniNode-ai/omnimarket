# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12998 — instruction-eval aggregate projection (TDD).

Tests written BEFORE the implementation.  They pin:

  1. The contract declares the canonical projection topic
     onex.snapshot.projection.omnimarket.instruction-eval-aggregate.v1
  2. The contract declares instruction_eval_aggregate_snapshots as a db_io
     write model
  3. The row builder maps an instruction-eval-result event to a snapshot row
     keyed on (model, task, context_mode) with pass_rate / output_tokens /
     runs fields
  4. When pass_rate is absent the row stores None (not a fake 0)
  5. The runner UPSERTs a snapshot row from an instruction-eval-result event
     via self.db.execute
  6. projection_api section in contract exposes the snapshot topic with the
     required columns
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

CONTRACT_PATH = (
    Path(__file__).parent.parent
    / "src/omnimarket/nodes/node_projection_instruction_eval/contract.yaml"
)

PROJECTION_TOPIC = "onex.snapshot.projection.omnimarket.instruction-eval-aggregate.v1"
INPUT_TOPIC = "onex.evt.omnimarket.instruction-eval-result.v1"
TABLE = "instruction_eval_aggregate_snapshots"


def _contract() -> dict[str, object]:
    return yaml.safe_load(CONTRACT_PATH.read_text())


# ---------------------------------------------------------------------------
# 1. Contract projection topic
# ---------------------------------------------------------------------------


def test_contract_declares_correct_projection_topic() -> None:
    api = _contract()["projection_api"]
    assert api["topic"] == PROJECTION_TOPIC, (
        f"projection_api.topic must be {PROJECTION_TOPIC!r}"
    )


# ---------------------------------------------------------------------------
# 2. Contract declares the write model
# ---------------------------------------------------------------------------


def test_contract_declares_write_table() -> None:
    tables = _contract()["db_io"]["db_tables"]
    write_tables = [t["name"] for t in tables if t.get("access") == "write"]
    assert TABLE in write_tables, (
        f"contract must declare {TABLE!r} as a write model; got {write_tables!r}"
    )


# ---------------------------------------------------------------------------
# 3. Row builder maps event to snapshot row
# ---------------------------------------------------------------------------


def test_row_builder_maps_pass_rate_output_tokens_runs() -> None:
    from omnimarket.nodes.node_projection_instruction_eval.handlers.row_instruction_eval import (
        build_instruction_eval_row,
    )

    row = build_instruction_eval_row(
        {
            "model": "ds4-flash",
            "task": "python-version",
            "context_mode": "chunk",
            "pass_rate": 1.0,
            "output_tokens": 314,
            "runs": 5,
        }
    )
    assert row["model"] == "ds4-flash"
    assert row["task"] == "python-version"
    assert row["context_mode"] == "chunk"
    assert float(row["pass_rate"]) == 1.0
    assert row["output_tokens"] == 314
    assert row["runs"] == 5


# ---------------------------------------------------------------------------
# 4. Absent pass_rate stored as None (never fake zero)
# ---------------------------------------------------------------------------


def test_row_builder_absent_pass_rate_is_none() -> None:
    from omnimarket.nodes.node_projection_instruction_eval.handlers.row_instruction_eval import (
        build_instruction_eval_row,
    )

    row = build_instruction_eval_row(
        {
            "model": "qwen-27b",
            "task": "git-commit-style",
            "context_mode": "baseline",
            # no pass_rate
            "output_tokens": 604,
            "runs": 5,
        }
    )
    assert row["pass_rate"] is None, (
        "absent pass_rate must be stored as None, never a fake 0"
    )


# ---------------------------------------------------------------------------
# 5. Runner UPSERTs snapshot row
# ---------------------------------------------------------------------------


def test_runner_upserts_snapshot_row_from_instruction_eval_result_event() -> None:
    from omnimarket.nodes.node_projection_instruction_eval.handlers.handler_instruction_eval import (
        InstructionEvalProjectionRunner,
    )
    from omnimarket.projection.runner import MessageMeta

    captured: list[tuple[object, ...]] = []

    class _RecordingDB:
        async def execute(self, *args: object, **kwargs: object) -> None:
            captured.append(args)

    runner = InstructionEvalProjectionRunner()
    runner._db = _RecordingDB()  # type: ignore[assignment]

    data: dict[str, object] = {
        "model": "qwen-35b",
        "task": "strongly-typed-models",
        "context_mode": "chunk",
        "pass_rate": 1.0,
        "output_tokens": 3814,
        "runs": 5,
    }
    meta = MessageMeta(partition=0, offset=0, fallback_id="eval-001")

    ok = asyncio.run(runner.project_event(INPUT_TOPIC, data, meta))
    assert ok is True, "project_event must return True after a successful upsert"
    assert len(captured) == 1, "exactly one upsert must be issued"

    sql = str(captured[0][0])
    assert f"INSERT INTO {TABLE}" in sql
    assert "ON CONFLICT" in sql
    params = captured[0][1:]
    assert "qwen-35b" in params
    assert "strongly-typed-models" in params
    assert "chunk" in params


# ---------------------------------------------------------------------------
# 6. projection_api exposes required columns
# ---------------------------------------------------------------------------


def test_contract_projection_api_exposes_required_columns() -> None:
    required = {"model", "task", "context_mode", "pass_rate", "output_tokens", "runs"}
    columns = set(_contract()["projection_api"]["columns"])
    missing = required - columns
    assert not missing, (
        f"projection_api.columns is missing required fields: {missing!r}"
    )


def test_contract_subscribes_to_instruction_eval_result_topic() -> None:
    subscribe = _contract()["event_bus"]["subscribe_topics"]
    assert INPUT_TOPIC in subscribe, f"contract must subscribe to {INPUT_TOPIC!r}"


@pytest.mark.parametrize(
    "context_mode",
    ["baseline", "chunk", "full-claude-md"],
)
def test_row_builder_handles_all_context_modes(context_mode: str) -> None:
    from omnimarket.nodes.node_projection_instruction_eval.handlers.row_instruction_eval import (
        build_instruction_eval_row,
    )

    row = build_instruction_eval_row(
        {
            "model": "ds4-flash",
            "task": "git-commit-style",
            "context_mode": context_mode,
            "pass_rate": 0.6667,
            "output_tokens": 822,
            "runs": 5,
        }
    )
    assert row["context_mode"] == context_mode
