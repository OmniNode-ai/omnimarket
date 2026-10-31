# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for ``node_projection_delegation_budget_readings`` (OMN-21326 AC3, OMN-17427).

node_delegate_skill_orchestrator on the deployed lane (one terminal per run)
    -> onex.evt.omnimarket.delegate-skill-completed.v1 / delegate-skill-failed.v1
    -> omninode_internal.delegation_budget_readings               (one row per id)
    -> onex.evt.omnimarket.projection-delegation-budget-readings-applied.v1 (terminal)
    -> handler_budget_readback, from any lab host                 (exit 0, 1, 2 or 3)
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_delegate_skill_orchestrator import runtime_budget_readback
from omnimarket.nodes.node_delegate_skill_orchestrator.models import ModelBudgetReading
from omnimarket.nodes.node_projection_delegation_budget_readings.handlers.handler_delegation_budget_readings_writer import (
    DelegationBudgetReadingsProjectionWriter,
)
from tests.test_omn21326_delegation_budget_readings_projection import (
    CORRELATION_ID,
    T0,
    _delivery,
)
from tests.test_omn21326_delegation_budget_readings_writer import _Db, _writer

pytestmark = pytest.mark.unit

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation_budget_readings"
    / "contract.yaml"
)
_COMPLETED = "onex.evt.omnimarket.delegate-skill-completed.v1"
_FAILED = "onex.evt.omnimarket.delegate-skill-failed.v1"
_TERMINAL = "onex.evt.omnimarket.projection-delegation-budget-readings-applied.v1"
_DLQ = "onex.dlq.omnimarket.projection-delegation-budget-readings-malformed.v1"


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_golden_chain_hops_are_the_declared_topics() -> None:
    contract = _contract()
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [_COMPLETED, _FAILED]
    assert bus["publish_topics"] == [_TERMINAL]
    assert bus["dlq_topics"] == [_DLQ]
    assert contract["terminal_event"] == _TERMINAL


def test_golden_chain_the_key_is_the_correlation_id() -> None:
    db_io = _contract()["db_io"]
    assert db_io["dedupe_key"] == ["correlation_id"]
    assert db_io["ordering_key"] == "observed_at"


def test_golden_chain_only_the_writer_is_routed() -> None:
    routed = [
        entry["handler"]["name"] for entry in _contract()["handler_routing"]["handlers"]
    ]
    assert routed == ["DelegationBudgetReadingsProjectionWriter"]
    assert DelegationBudgetReadingsProjectionWriter.onex_runtime_inprocess_dispatch


def test_golden_chain_an_over_budget_terminal_fails_the_readback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Terminal on the bus -> the row the writer stores -> the readback's exit 1."""
    db = _Db()
    out = _writer(db, monkeypatch).handle(
        _delivery(T0 - timedelta(hours=1), execution_duration_ms=65_001)
    )
    assert out["rows_upserted"] == 1
    stored = db.calls[-1][1]

    class _Stored:
        def describe(self) -> str:
            return "rows the writer stored"

        def read(self, since: Any, until: Any) -> list[ModelBudgetReading]:
            assert since <= stored[10] < until
            return [
                ModelBudgetReading(
                    run_id=str(stored[0]),
                    status=stored[1],
                    requested_timeout_seconds=stored[4],
                    execution_timeout_seconds=stored[5],
                    execution_duration_ms=stored[6],
                )
            ]

    code = runtime_budget_readback.main(
        ["--until", T0.isoformat()], projection=_Stored()
    )
    summary = json.loads(capsys.readouterr().out)

    assert code == 1
    assert summary["over_runs"] == [
        {
            "run_id": str(CORRELATION_ID),
            "execution_duration_ms": 65_001,
            "bound_ms": 65_000,
        }
    ]
