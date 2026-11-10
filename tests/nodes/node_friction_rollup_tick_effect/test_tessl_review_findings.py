# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings on the friction rollup tick: report references and trend keys."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_friction_rollup_tick_effect.handlers.friction_rollup_tick_core import (
    compute_trend,
    status_row,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.handlers.handler_friction_rollup_tick import (
    DEFAULT_OVERLAY,
    HandlerFrictionRollupTick,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.models.model_friction_rollup_tick import (
    ModelFrictionRollupTickRequest,
    ModelFrictionRollupTickSettings,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.protocols.protocol_ledger_appender import (
    InMemoryLedgerAppender,
)
from tests.nodes.node_friction_rollup_tick_effect.tick_support import (
    install_ledger,
    request_for,
)

pytestmark = pytest.mark.unit
NOW = "2026-09-26T12:00:00Z"


def _settings(**changes: object) -> ModelFrictionRollupTickSettings:
    document = yaml.safe_load(DEFAULT_OVERLAY.read_text(encoding="utf-8"))
    document.update(changes)
    return ModelFrictionRollupTickSettings.model_validate(document)


# --- the STATUS references name the effective report location --------------------------------------------------


def _row(tmp_path: Path, state_dir: Path | None) -> str:
    request = ModelFrictionRollupTickRequest(
        registry_root=str(tmp_path / "registry"),
        ledger_path=str(install_ledger(tmp_path)),
        now=NOW,
        dry_run=True,
        state_dir=None if state_dir is None else str(state_dir),
    )
    return (
        HandlerFrictionRollupTick(appender=InMemoryLedgerAppender())
        .handle(request)
        .status_row
    )


def test_status_references_follow_the_state_dir_override(tmp_path: Path) -> None:
    custom = tmp_path / "elsewhere"
    row = _row(tmp_path, custom)
    assert row.endswith(
        f"report={custom}/20260926T120000Z.md json={custom}/latest.json"
    )


def test_status_references_keep_the_relative_form_for_the_default_location(
    tmp_path: Path,
) -> None:
    expected = (
        "report=.onex_state/friction-rollup/20260926T120000Z.md"
        " json=.onex_state/friction-rollup/latest.json"
    )
    assert _row(tmp_path, None).endswith(expected)
    default = tmp_path / "registry" / ".onex_state" / "friction-rollup"
    assert _row(tmp_path, default).endswith(expected)


# --- the JSON trend keys carry the configured window names -----------------------------------------------------


def _report(rows: int, cost: float) -> dict[str, object]:
    return {
        "classes": [],
        "totals": {"friction_rows": rows, "occurrences": rows, "cost_minutes": cost},
        "window": {"since": "s", "until": "u"},
    }


def test_trend_keys_follow_the_configured_window_names() -> None:
    settings = _settings(
        windows=[{"name": "48h", "hours": 48}, {"name": "6h", "hours": 6}]
    )
    previous = {
        "generated_at": "g",
        "windows": {"48h": _report(10, 100.0), "6h": _report(2, 20.0)},
    }
    current = {"48h": _report(15, 130.0), "6h": _report(3, 25.0)}
    trend = compute_trend(current, [], [], previous, "p.json", settings)
    assert "totals_24h_delta" not in trend
    assert "totals_2h_delta" not in trend
    assert trend["totals_48h_delta"] == {
        "friction_rows": 5.0,
        "occurrences": 5.0,
        "cost_minutes": 30.0,
    }
    assert trend["totals_6h_delta"] == {"friction_rows": 1.0, "cost_minutes": 5.0}
    summary = {"top_classes": [], "needs_ticket": [], "trend": trend}
    now = datetime(2026, 9, 26, tzinfo=UTC)
    row = status_row(now, current, summary, "r.md", settings)
    assert "rows +5, occurrences +5, cost +30 min (6h cost +5 min)" in row


def test_second_tick_of_a_custom_profile_stores_deltas_under_its_window_names(
    tmp_path: Path,
) -> None:
    handler = HandlerFrictionRollupTick(
        appender=InMemoryLedgerAppender(),
        settings=_settings(
            windows=[{"name": "48h", "hours": 48}, {"name": "6h", "hours": 6}]
        ),
    )
    ledger = install_ledger(tmp_path)
    handler.handle(request_for(tmp_path, ledger, "2026-09-26T06:00:00Z"))
    second = handler.handle(request_for(tmp_path, ledger, NOW))
    trend = json.loads(second.report_json_text)["summary"]["trend"]
    assert {"totals_48h_delta", "totals_6h_delta"} <= set(trend)
    assert not {"totals_24h_delta", "totals_2h_delta"} & set(trend)
    assert "- 48h: rows " in second.report_markdown_text
    assert "- 6h: rows " in second.report_markdown_text


def test_default_profile_keeps_the_recorded_trend_keys() -> None:
    settings = _settings()
    previous = {
        "generated_at": "g",
        "windows": {"24h": _report(1, 1.0), "2h": _report(1, 1.0)},
    }
    current = {"24h": _report(2, 2.0), "2h": _report(2, 2.0)}
    trend = compute_trend(current, [], [], previous, "p.json", settings)
    assert {"totals_24h_delta", "totals_2h_delta"} <= set(trend)
    json.dumps(trend)
