# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay the scheduled R8 and R9 exits with a stubbed dev readiness response."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from omnimarket.handlers import delegation_readiness_scenario
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioConclusion,
    ModelScenarioResult,
    from_status_row,
)
from omnimarket.models.overlay_keys.model_overlay_documents import (
    ModelLabHostRow,
    ModelLabHostsTable,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect import runtime_scenario_r8_tick
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
    HandlerScenarioR8,
)
from omnimarket.nodes.node_delegation_scenario_r9_effect import runtime_scenario_r9_tick
from omnimarket.nodes.node_delegation_scenario_r9_effect.handlers.handler_scenario_r9 import (
    HandlerScenarioR9,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scenario", "tick", "handler"),
    [
        ("R8", runtime_scenario_r8_tick, HandlerScenarioR8),
        ("R9", runtime_scenario_r9_tick, HandlerScenarioR9),
    ],
)
@pytest.mark.parametrize(
    ("ready", "append_outcome"),
    [(False, "accepted"), (False, "queued"), (True, "accepted")],
)
def test_scheduled_tick_checks_dev_before_entering_the_scenario(
    scenario: str,
    tick,
    handler,
    ready: bool,
    append_outcome: str,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    fixture = json.loads(
        Path(__file__)
        .with_name("fixtures")
        .joinpath("ready_unavailable_20261119.json")
        .read_text()
    )
    rows: list[str] = []
    calls: list[str] = []

    def readiness(*args, **kwargs) -> bool:
        calls.append("ready")
        return ready or bool(fixture["ready"]["available"])

    def run_product_check(self, assertions: list[str]) -> str:
        calls.append("scenario")
        assertions.append(
            "+checked" if ready else fixture["previous_assertions"][scenario]
        )
        return "scenario ran"

    def append(self, row: str, request_id: str) -> ModelAppendOutcome:
        rows.append(row)
        if append_outcome == "queued":
            return ModelAppendOutcome(status="error", message="receipt-status-queued")
        return ModelAppendOutcome(status="accepted")

    monkeypatch.setattr(CommandLedgerAppender, "append", append)
    monkeypatch.setattr(handler, "_scenario", run_product_check)
    monkeypatch.setattr(
        delegation_readiness_scenario,
        "wait_for_dev_readiness",
        readiness,
        raising=False,
    )
    monkeypatch.setattr(runtime_scenario_r8_tick, "r8_settings_store", lambda: None)
    monkeypatch.setenv("ONEX_LEDGER_BUS_APPEND_COMMAND", "test-append")
    monkeypatch.delenv("ONEX_BUS_EMIT_COMMAND", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    state = tmp_path / "state"

    code = tick.main(["--omni-home", str(tmp_path), "--state-dir", str(state)])

    result = ModelScenarioResult.model_validate_json(capsys.readouterr().out)
    expected = (
        EnumScenarioConclusion.SUCCESS if ready else EnumScenarioConclusion.SKIPPED
    )
    assert result.conclusion is expected
    assert code == 0
    assert calls == (["ready", "scenario"] if ready else ["ready"])
    assert len(rows) == 1
    ledger_result = from_status_row(rows[0])
    assert ledger_result is not None
    assert ledger_result.model_dump(exclude={"recorded_at"}) == result.model_dump(
        exclude={"recorded_at"}
    )
    assert json.loads((state / "last-run.json").read_text())["conclusion"] == expected
    assert result.assertions == (("+checked",) if ready else ())
    assert result.detail == ("scenario ran" if ready else "dev-not-ready")


@pytest.mark.unit
@pytest.mark.parametrize("final_status", [200, 503])
def test_readiness_polls_until_ready_or_deadline(
    final_status: int, monkeypatch
) -> None:
    now = 0.0
    attempts: list[float] = []

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://dev.example.test:8085/ready")
        attempts.append(now)
        return httpx.Response(503 if len(attempts) == 1 else final_status)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    hosts = ModelLabHostsTable(
        hosts=(ModelLabHostRow(name="dev", target="dev.example.test"),)
    )
    monkeypatch.setattr(
        delegation_readiness_scenario,
        "time",
        SimpleNamespace(monotonic=lambda: now, sleep=sleep),
    )
    monkeypatch.setattr(
        delegation_readiness_scenario, "resolve_lab_hosts_table", lambda _: hosts
    )
    monkeypatch.setattr(httpx, "Client", lambda **_: client)

    config = delegation_readiness_scenario.ModelDevReadiness(
        host="dev", port=8085, wait_seconds=2, poll_seconds=1
    )

    assert delegation_readiness_scenario.wait_for_dev_readiness(config, {}) is (
        final_status == 200
    )
    assert attempts == [0, 1]
    assert now == (1 if final_status == 200 else 2)
