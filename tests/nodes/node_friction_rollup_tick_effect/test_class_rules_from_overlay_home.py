# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The friction rollup tick passes the overlay home's class rules, so the stoppages the rules name are classed (OMN-22814)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_friction_rollup_tick_effect.handlers.handler_friction_rollup_tick import (
    HandlerFrictionRollupTick,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.models.model_friction_rollup_tick import (
    ModelFrictionRollupTickRequest,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.protocols.protocol_ledger_appender import (
    InMemoryLedgerAppender,
)
from tests.nodes.node_friction_rollup_tick_effect.conftest import (
    CLASS_OVERLAY_ENV,
    OVERLAY_HOME_FILE,
)
from tests.nodes.node_friction_rollup_tick_effect.tick_support import FIXTURES

pytestmark = pytest.mark.unit

# The 24 FRICTION rows recorded in the ledger window of 2026-11-07, none with a class= cell.
RECORDED_ROWS = FIXTURES.parent / "friction_class_rules_rows_2026_11_07.md"
NOW = "2026-11-08T00:00:00Z"
RECORDED_ROW_COUNT = 24


def _window_totals(
    tmp_path: Path, environ: dict[str, str]
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    result = HandlerFrictionRollupTick(
        appender=InMemoryLedgerAppender(), environ=environ
    ).handle(
        ModelFrictionRollupTickRequest(
            registry_root=str(tmp_path / "registry"),
            ledger_path=str(RECORDED_ROWS),
            now=NOW,
            dry_run=True,
            state_dir=str(tmp_path / "state"),
        )
    )
    window = json.loads(result.report_json_text)["windows"]["24h"]
    return (
        result.exit_code,
        window["totals"],
        {c["class"]: c for c in window["classes"]},
    )


def test_the_rules_of_the_overlay_home_class_the_recorded_rows(tmp_path: Path) -> None:
    exit_code, totals, classes = _window_totals(
        tmp_path, {CLASS_OVERLAY_ENV: str(OVERLAY_HOME_FILE)}
    )
    assert exit_code == 0
    assert totals["friction_rows"] == RECORDED_ROW_COUNT
    assert totals["rows_classified_by_rule"] == RECORDED_ROW_COUNT
    assert totals["rows_without_class"] == RECORDED_ROW_COUNT
    assert "unclassified" not in classes


def test_control_without_the_overlay_home_leaves_every_recorded_row_unclassified(
    tmp_path: Path,
) -> None:
    exit_code, totals, classes = _window_totals(tmp_path, {})
    assert exit_code == 3
    assert totals["friction_rows"] == RECORDED_ROW_COUNT
    assert "rows_classified_by_rule" not in totals
    assert classes["unclassified"]["rows"] == RECORDED_ROW_COUNT
