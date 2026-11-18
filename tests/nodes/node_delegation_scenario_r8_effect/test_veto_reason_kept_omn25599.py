# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An R8 delegation veto keeps its diagnostic in the job journal and run receipt."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.handlers.delegation_readiness_scenario import load_terminal_topic
from omnimarket.handlers.delegation_scenario_runtime import (
    JournalScenarioEventSink,
    LedgerAppender,
)
from omnimarket.models.model_delegation_scenario_settings import (
    ModelScenarioTenantReadOverlay,
)
from omnimarket.models.model_tenant_read_bus import (
    EnumTenantReadKind,
    ModelTenantReadCommand,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_lane import (
    BusLane,
    ModelTenantReadAnswer,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
    CONTRACT_PATH,
    HandlerScenarioR8,
    load_lane_settings,
    load_runtime,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.runtime_scenario_r8_tick import (
    main as run_tick,
)

pytestmark = pytest.mark.unit

RUNTIME = load_runtime()
SETTINGS = load_lane_settings()
NOW = datetime(2026, 11, 18, 8, 16, 4, tzinfo=UTC)
VETO_REASON = (
    "pre_filter_rejected: actual_score=1.000 required_bar=0.800 "
    "score_vs_bar=at_or_above_bar bar_source=task_class_gate_contract "
    "score_source=blocking_acceptance_and_model_evaluations "
    "deciding_rules=deliverable_extraction,required_fact_coverage; "
    "failures=expected report section missing; supporting evidence absent"
)
SEND_FAILURE = "send: the delegate command (exit 1) ended failed: "
CUT_MARK = " [truncated]"


def _runner(home: Path, append: LedgerAppender, reason: str) -> HandlerScenarioR8:
    def read_totals(
        command: ModelTenantReadCommand, _timeout: float
    ) -> ModelTenantReadAnswer:
        assert command.kind is EnumTenantReadKind.SAVINGS
        return ModelTenantReadAnswer(
            completed=True, body=dict.fromkeys(SETTINGS.totals_keys.values(), 0)
        )

    def delegate(argv: Sequence[str], _timeout: float) -> tuple[int, str]:
        assert "--json" in argv
        return 1, json.dumps(
            {
                "correlation_id": "vetoed-run",
                "status": "failed",
                "result": {"terminal_payload": {"payload": {"error_message": reason}}},
            }
        )

    overlay = ModelScenarioTenantReadOverlay(
        bus="inmemory",
        bus_lane="dev",
        probe_tenant_id=UUID(int=1),
        timeout_seconds=5,
    )
    return HandlerScenarioR8(
        RUNTIME,
        home=home,
        environment={},
        appender=append,
        sink=JournalScenarioEventSink(home / RUNTIME.event_journal_relpath),
        terminal_topic=load_terminal_topic(CONTRACT_PATH),
        lane_factory=lambda: BusLane(
            SETTINGS, overlay, read_totals, run_command=delegate
        ),
        clock=lambda: NOW,
    )


def _journal_payload(home: Path) -> dict[str, object]:
    journal = home / RUNTIME.event_journal_relpath
    (line,) = journal.read_text(encoding="utf-8").splitlines()
    event = json.loads(line)
    assert event["topic"] == load_terminal_topic(CONTRACT_PATH)
    return event["payload"]


def _receipt(home: Path) -> dict[str, object]:
    receipts = home / RUNTIME.work_relpath / "receipts"
    (receipt,) = receipts.glob("R8-*.json")
    return json.loads(receipt.read_text(encoding="utf-8"))


def test_long_veto_survives_in_the_r8_job_record_and_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert len(VETO_REASON) > 200
    rows: list[str] = []
    runner = _runner(tmp_path, rows.append, VETO_REASON)

    exit_code = run_tick(
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "tick")],
        build=lambda _env, _trigger: runner,
    )

    assert exit_code == 1
    result = _journal_payload(tmp_path)
    assert result["conclusion"] == "failure"
    assert result["assertions"] == [
        "+inputs_readable",
        "+lane_configured",
        "+totals_read_before",
        "-runs_sent",
    ]
    assert result["detail"] == SEND_FAILURE + VETO_REASON
    assert _receipt(tmp_path) == result
    assert json.loads(capsys.readouterr().out) == result


@pytest.mark.parametrize("length", [240, 241])
def test_status_row_marks_only_shortened_detail(tmp_path: Path, length: int) -> None:
    reason = "veto " + "x" * (length - len(SEND_FAILURE) - len("veto "))
    detail = SEND_FAILURE + reason
    rows: list[str] = []

    result = _runner(tmp_path, rows.append, reason).run()

    (row,) = rows
    status_detail = row.rsplit(" | ", 1)[1]
    expected = detail if length == 240 else detail[: 240 - len(CUT_MARK)] + CUT_MARK
    assert status_detail == expected
    assert result.detail == detail


def test_overlong_job_detail_has_an_explicit_cut_mark(tmp_path: Path) -> None:
    reason = VETO_REASON + "x" * 8000
    expected = (SEND_FAILURE + reason)[: 8000 - len(CUT_MARK)] + CUT_MARK

    _runner(tmp_path, lambda _row: None, reason).run()

    detail = _journal_payload(tmp_path)["detail"]
    assert isinstance(detail, str)
    assert len(detail) == 8000
    assert detail == expected
    assert _receipt(tmp_path)["detail"] == expected


def test_receipt_survives_a_ledger_append_failure(tmp_path: Path) -> None:
    def refuse_append(_row: str) -> None:
        raise RuntimeError("ledger append refused")

    with pytest.raises(RuntimeError, match="ledger append refused"):
        _runner(tmp_path, refuse_append, VETO_REASON).run()

    assert _receipt(tmp_path)["detail"] == SEND_FAILURE + VETO_REASON
    assert not (tmp_path / RUNTIME.event_journal_relpath).exists()


def test_runs_with_the_same_timestamp_each_keep_a_receipt(tmp_path: Path) -> None:
    reasons = (VETO_REASON, "another veto")
    for reason in reasons:
        _runner(tmp_path, lambda _row: None, reason).run()

    receipts = (tmp_path / RUNTIME.work_relpath / "receipts").glob("R8-*.json")
    recorded = {
        json.loads(path.read_text(encoding="utf-8"))["detail"] for path in receipts
    }
    assert recorded == {SEND_FAILURE + reason for reason in reasons}
