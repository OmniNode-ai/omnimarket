# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC8: the backend serves one run duration with its definition.

Failure modes, each with a test below:
  R1  the metering summary serves no run duration, so each page picks its own column
  R2  the served column is one the decisions topic, which Runs and the run detail read, does not serve
  R3  the served duration names the model call time (latency_ms), which a failed call stores as 0
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_metering_summary_compute.models.model_run_duration import (
    RUN_DURATION_DEFINITION,
)
from omnimarket.projection.sqlite_metering_summary import (
    refresh_metering_summary,
    resolve_metering_baseline_model,
)
from tests.unit.projection.test_sqlite_delegation_savings_view_omn20008 import (
    _TENANT,
    _call,
    _delegation,
    _store,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 16, tzinfo=UTC)
_CONTRACT = Path("src/omnimarket/nodes/node_projection_delegation/contract.yaml")
_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"


def _served_run_duration(tmp_path: Path) -> dict[str, str]:
    path = _store(
        tmp_path, (_delegation("measured"),), (_call("measured", "measured"),)
    )
    rows = refresh_metering_summary(
        path, _TENANT, resolve_metering_baseline_model().model, _NOW
    )
    [row] = [r for r in rows if r.window_kind == "all"]
    served: dict[str, str] = json.loads(row.summary_json)["run_duration"]
    return served


def _decisions_columns() -> list[str]:
    contract = yaml.safe_load(_CONTRACT.read_text())
    found: list[list[str]] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("topic") == _DECISIONS and "columns" in node:
                found.append(list(node["columns"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(contract)
    assert found, "the decisions exposure was not found in the contract"
    return found[0]


def test_run_duration_definition_is_served(tmp_path: Path) -> None:
    """R1."""
    assert _served_run_duration(tmp_path) == {
        "column": "execution_ms",
        "label": "Run time",
        "definition": RUN_DURATION_DEFINITION,
    }


def test_run_duration_definition_column_is_on_the_decisions_topic(
    tmp_path: Path,
) -> None:
    """R2."""
    assert _served_run_duration(tmp_path)["column"] in _decisions_columns()


def test_run_duration_definition_is_not_the_model_call_time(tmp_path: Path) -> None:
    """R3."""
    assert _served_run_duration(tmp_path)["column"] != "latency_ms"
