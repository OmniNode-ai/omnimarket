# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed read-only handler replay of the actual captured plugin walk."""

import json
import sqlite3
from pathlib import Path

import pytest

from omnimarket.nodes.node_plugin_walk_evidence_effect import HandlerPluginWalkEvidence
from omnimarket.nodes.node_plugin_walk_evidence_effect.models.model_plugin_walk_evidence import (
    ModelPluginWalkEvidenceRequest,
)

pytestmark = pytest.mark.unit


def test_golden_chain_plugin_walk(tmp_path: Path) -> None:
    walk = json.loads(
        (
            Path(__file__).parents[2] / "fixtures/c30_plugin_walk_2026_10_25.json"
        ).read_text()
    )
    for arm in ["positive", "negative"]:
        (tmp_path / (arm + ".jsonl")).write_text(
            "\n".join(json.dumps(e) for e in walk[arm])
        )
    database = tmp_path / "readback.sqlite"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE delegation_events (correlation_id TEXT, terminal_ok INTEGER)"
        )
        conn.execute(
            "CREATE TABLE llm_call_metrics (correlation_id TEXT, total_tokens INTEGER)"
        )
        for cid, rows in walk["readback"].items():
            for row in rows["delegation_events"]:
                conn.execute(
                    "INSERT INTO delegation_events VALUES (?, ?)",
                    (cid, row["terminal_ok"]),
                )
            for row in rows["llm_call_metrics"]:
                conn.execute(
                    "INSERT INTO llm_call_metrics VALUES (?, ?)",
                    (cid, row["total_tokens"]),
                )
    result = HandlerPluginWalkEvidence().handle(
        ModelPluginWalkEvidenceRequest(
            positive=tmp_path / "positive.jsonl",
            negative=tmp_path / "negative.jsonl",
            database=database,
        )
    )
    assert result.positive_correlation_ids == tuple(walk["readback"])
    assert result.negative_correlation_ids == ()
