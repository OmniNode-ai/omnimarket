# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders for the piece 16 reading tests (OMN-22980): the overlay comes from the node's
own overlay example, so no threshold is spelled in a test."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.models.model_context_recall_reading import (
    ModelContextRecallOverlay,
    ModelContextRecallReadRequest,
)

NODE = "omnimarket.nodes.node_context_recall_reading_compute"
NOW = datetime(2026, 11, 7, 14, 0, 0, tzinfo=UTC)
FACT = "the launch codeword is heron-4471"


def overlay_data() -> dict[str, Any]:
    loaded = yaml.safe_load(files(NODE).joinpath("overlay.example.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def overlay() -> ModelContextRecallOverlay:
    return ModelContextRecallOverlay.model_validate(overlay_data())


def ago(seconds: int) -> str:
    return (NOW - timedelta(seconds=seconds)).isoformat()


def probe(
    probe_id: str = "p1",
    *,
    recalled_answer: str | None = f"You told me {FACT}.",
    compaction_forced: bool = True,
    compaction_done: bool = True,
    planted: int = 1800,
    compacted: int = 1200,
    read: int = 600,
) -> dict[str, Any]:
    """A planted fact told ``planted`` seconds before now, compacted, then asked for again."""
    return {
        "probe_id": probe_id,
        "expected_fact": FACT,
        "planted_at": ago(planted),
        "compaction_forced": compaction_forced,
        "compaction_completed_at": ago(compacted) if compaction_done else None,
        "recalled_answer": recalled_answer,
        "recall_read_at": ago(read) if recalled_answer is not None else None,
    }


def eval_run(
    run_id: str = "e1",
    *,
    started: int = 900,
    ended: int | None = 300,
    result_id: str | None = "result-1",
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "started_at": ago(started),
        "ended_at": ago(ended) if ended is not None else None,
        "result_id": result_id,
    }


def request_data(
    probes: list[dict[str, Any]] | None = None,
    eval_runs: list[dict[str, Any]] | None = None,
    correlation_id: str = "chain-16",
) -> dict[str, Any]:
    return {
        "correlation_id": correlation_id,
        "now": NOW.isoformat(),
        "overlay": overlay_data(),
        "probes": [probe()] if probes is None else probes,
        "eval_runs": [eval_run()] if eval_runs is None else eval_runs,
    }


def request(
    probes: list[dict[str, Any]] | None = None,
    eval_runs: list[dict[str, Any]] | None = None,
) -> ModelContextRecallReadRequest:
    return ModelContextRecallReadRequest.model_validate(request_data(probes, eval_runs))
