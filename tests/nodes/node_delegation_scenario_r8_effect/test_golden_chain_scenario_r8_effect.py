# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of scenario R8 (OMN-23175): one run writes one STATUS row and emits one result on the bus.

The golden chains run the scenario against the in-memory lane model (a lane that keeps synthetic runs out of the totals) and
against the real HTTP backend with an injected transport that answers like the tenant API. The error chains are named injected
breaks: each makes exactly one thing wrong and shows the scenario red for that reason. The failing control is the lane that
counts a synthetic run: it must be refused.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from omnimarket.handlers.delegation_scenario_runtime import (
    InMemoryScenarioEventSink,
    JournalScenarioEventSink,
    render_systemd,
)
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    from_status_row,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_lane import (
    HttpLane,
    InMemoryLane,
    LaneReadError,
    ModelLaneSettings,
    ModelRunRow,
    ModelSendRequest,
    ModelTotals,
    ProtocolR8Lane,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
    CONTRACT_PATH,
    NODE_DIR,
    HandlerScenarioR8,
    ModelScenarioR8RunCommand,
    build_runner,
    load_lane_settings,
    load_runtime,
    load_schedule,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.runtime_scenario_r8 import (
    main as r8_main,
)

pytestmark = pytest.mark.unit

TERMINAL = "onex.evt.omnimarket.delegation-scenario-result.v1"
NOW = datetime(2026, 11, 9, 13, 6, 30, tzinfo=UTC)
RUNTIME = load_runtime()
SETTINGS = load_lane_settings()
EVERY_ASSERTION = (
    "inputs_readable",
    "lane_configured",
    "totals_read_before",
    "runs_sent_distinct",
    "projection_holds_both",
    "totals_read_after",
    "synthetic_listed_labelled_synthetic",
    "organic_listed_labelled_organic",
    "organic_run_has_values",
    "run_total_rose_by_one",
    "token_total_rose_by_organic_tokens",
    "spend_total_rose_by_organic_cost",
    "savings_total_rose_by_organic_saved",
)


class Rows:
    def __init__(self) -> None:
        self.rows: list[str] = []

    def __call__(self, row: str) -> None:
        self.rows.append(row)


class Clock:
    """A clock the injected sleep advances, so a wait that runs out costs no real time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def build(
    tmp_path: Path,
    lane: ProtocolR8Lane,
    *,
    inputs_path: Path | None = None,
    sink: InMemoryScenarioEventSink | None = None,
    clock: Clock | None = None,
    trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
) -> tuple[HandlerScenarioR8, Rows, InMemoryScenarioEventSink]:
    rows = Rows()
    events = sink or InMemoryScenarioEventSink()
    ticker = clock or Clock()
    handler = HandlerScenarioR8(
        RUNTIME,
        home=tmp_path,
        environment={},
        appender=rows,
        sink=events,
        terminal_topic=TERMINAL,
        lane_factory=lambda: lane,
        trigger=trigger,
        clock=lambda: NOW,
        inputs_path=inputs_path,
        sleep=ticker.sleep,
        monotonic=ticker.monotonic,
    )
    return handler, rows, events


def failed(assertions: tuple[str, ...]) -> list[str]:
    return [item for item in assertions if item.startswith("-")]


# -- golden chains --------------------------------------------------------------------------------------------------


def test_golden_chain_inmemory_every_assertion_held(tmp_path: Path) -> None:
    handler, rows, events = build(tmp_path, InMemoryLane())

    result = handler.handle(ModelScenarioR8RunCommand())

    assert failed(result.assertions) == [], result.detail
    assert result.conclusion is EnumScenarioConclusion.SUCCESS
    assert tuple(item[1:] for item in result.assertions) == EVERY_ASSERTION
    assert len(rows.rows) == 1
    parsed = from_status_row(rows.rows[0])
    assert parsed is not None
    assert parsed.scenario_id == "R8"
    assert parsed.trigger is EnumScenarioTrigger.SCHEDULE
    assert parsed.slot_start == datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    assert [(topic, payload["scenario_id"]) for topic, payload in events.events] == [
        (TERMINAL, "R8")
    ]


class FakeTenantApi:
    """What the tenant API and the delegate command answer, with the totals a lane that excludes synthetic runs holds."""

    def __init__(self, *, count_synthetic: bool = False, drop_key: str | None = None):
        self.model = InMemoryLane(count_synthetic=count_synthetic)
        self.drop_key = drop_key
        self.commands: list[list[str]] = []

    def get(self, url: str, headers: Mapping[str, str], timeout: float) -> object:
        assert headers == {"x-api-key": "probe-key"}
        assert timeout == SETTINGS.request_timeout_seconds
        if url.startswith("https://api.example/v1/tenants/me/savings"):
            totals = self.model.read_totals()
            body: dict[str, object] = {
                "delegation_runs": totals.runs,
                "total_tokens": totals.tokens,
                "total_spend_usd": float(totals.spend_usd),
                "total_saved_usd": float(totals.savings_usd),
            }
            body.pop(self.drop_key or "", None)
            return body
        if url.startswith("https://api.example/v1/tenants/me/delegations?limit=50"):
            return {
                "runs": [
                    {
                        "run_id": row.correlation_id,
                        "traffic_class": row.traffic_class,
                        "total_tokens": row.tokens,
                        "cost_usd": float(row.cost_usd),
                        "saved_usd": float(row.saved_usd),
                    }
                    for row in self.model.read_runs()
                ]
            }
        raise AssertionError(url)

    def run(self, argv: Sequence[str], timeout: float) -> tuple[int, str]:
        self.commands.append(list(argv))
        flags = list(argv)
        traffic = (
            flags[flags.index("--traffic-class") + 1]
            if "--traffic-class" in flags
            else None
        )
        surface = (
            flags[flags.index("--source-surface") + 1]
            if "--source-surface" in flags
            else None
        )
        correlation_id = self.model.send(
            ModelSendRequest(
                task_type=flags[flags.index("--task-type") + 1],
                prompt=flags[2],
                traffic_class=traffic,
                source_surface=surface,
            )
        )
        return 0, json.dumps({"correlation_id": correlation_id})


def http_lane(api: FakeTenantApi) -> HttpLane:
    return HttpLane(
        SETTINGS,
        {
            "R8_PROBE_API_BASE_URL": "https://api.example/",
            "R8_PROBE_TENANT_API_KEY": "probe-key",
        },
        http_get=api.get,
        run_command=api.run,
    )


def test_golden_chain_real_http_backend_sends_the_flags_and_reads_the_api(
    tmp_path: Path,
) -> None:
    api = FakeTenantApi()
    handler, rows, events = build(tmp_path, http_lane(api))

    result = handler.run()

    assert failed(result.assertions) == [], result.detail
    synthetic_command, organic_command = api.commands
    assert synthetic_command[:2] == ["onex", "delegate"]
    assert synthetic_command[-4:] == [
        "--traffic-class",
        "synthetic",
        "--source-surface",
        "readiness_probe",
    ]
    assert "--traffic-class" not in organic_command
    assert "--source-surface" not in organic_command
    assert len(rows.rows) == 1
    assert len(events.events) == 1


def test_golden_chain_manual_trigger_is_recorded_as_manual(tmp_path: Path) -> None:
    handler, rows, _ = build(
        tmp_path, InMemoryLane(), trigger=EnumScenarioTrigger.MANUAL
    )
    handler.run()
    parsed = from_status_row(rows.rows[0])
    assert parsed is not None
    assert parsed.trigger is EnumScenarioTrigger.MANUAL


def test_golden_chain_event_is_journalled(tmp_path: Path) -> None:
    journal = tmp_path / "events.jsonl"
    handler = HandlerScenarioR8(
        RUNTIME,
        home=tmp_path,
        environment={},
        appender=Rows(),
        sink=JournalScenarioEventSink(journal),
        terminal_topic=TERMINAL,
        lane_factory=InMemoryLane,
        clock=lambda: NOW,
    )
    handler.run()
    lines = journal.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["topic"] == TERMINAL


def test_golden_chain_the_record_run_writes_the_row_to_the_file_not_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv("ONEX_LEDGER_BUS_APPEND_COMMAND", raising=False)
    monkeypatch.delenv("ONEX_BUS_EMIT_COMMAND", raising=False)
    record = tmp_path / "out" / "readiness-r8-record.txt"

    handler = build_runner(record=record)
    assert (
        handler._lane_factory.__name__ == "lane_factory"
    )  # the contract's lane, not a test double
    with pytest.raises(LaneReadError, match="R8_PROBE_API_BASE_URL"):
        handler._lane_factory()

    # An unconfigured lane is a failed run that still writes its row: the record holds exactly one STATUS row.
    result = handler.run()
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert result.assertions[-1] == "-lane_configured"
    rows = record.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    parsed = from_status_row(rows[0])
    assert parsed is not None
    assert parsed.scenario_id == "R8"


def test_the_cli_run_with_a_record_exits_one_for_an_unconfigured_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv("R8_PROBE_API_BASE_URL", raising=False)
    monkeypatch.delenv("R8_PROBE_TENANT_API_KEY", raising=False)
    record = tmp_path / "record.txt"

    code = r8_main(["run", "--manual", "--record", str(record)])

    assert code == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["scenario_id"] == "R8"
    assert printed["trigger"] == "manual"
    assert "-lane_configured" in printed["assertions"]
    assert len(record.read_text(encoding="utf-8").splitlines()) == 1


# -- error chains: one named injected break each, and the failing control ----------------------------------------------


def test_failing_control_a_lane_that_counts_a_synthetic_run_is_refused(
    tmp_path: Path,
) -> None:
    """The control: the lane counts the synthetic run, so the totals rise by two runs and the scenario must go red."""
    handler, rows, _ = build(tmp_path, InMemoryLane(count_synthetic=True))
    result = handler.run()
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert "-run_total_rose_by_one" in result.assertions
    assert "-token_total_rose_by_organic_tokens" in result.assertions
    assert "-spend_total_rose_by_organic_cost" in result.assertions
    assert "-savings_total_rose_by_organic_saved" in result.assertions
    assert "+synthetic_listed_labelled_synthetic" in result.assertions
    assert "runs rose by 2" in result.detail
    assert len(rows.rows) == 1


def test_failing_control_over_the_real_http_backend(tmp_path: Path) -> None:
    handler, _, _ = build(tmp_path, http_lane(FakeTenantApi(count_synthetic=True)))
    result = handler.run()
    assert "-run_total_rose_by_one" in result.assertions
    assert result.conclusion is EnumScenarioConclusion.FAILURE


def test_error_chain_a_synthetic_row_labelled_organic_turns_the_label_red(
    tmp_path: Path,
) -> None:
    handler, _, _ = build(tmp_path, InMemoryLane(label_synthetic=False))
    result = handler.run()
    assert "-synthetic_listed_labelled_synthetic" in result.assertions
    assert "+organic_listed_labelled_organic" in result.assertions


def test_error_chain_a_synthetic_run_missing_from_runs_times_out_and_is_red(
    tmp_path: Path,
) -> None:
    clock = Clock()
    handler, rows, _ = build(tmp_path, InMemoryLane(list_synthetic=False), clock=clock)
    result = handler.run()
    assert result.assertions[-1] == "-projection_holds_both"
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert (
        clock.now >= 240
    )  # it waited the whole declared wait, in poll steps, before it gave up
    assert set(clock.slept) == {10.0}
    assert len(rows.rows) == 1


def test_error_chain_an_organic_control_with_no_values_cannot_pass_vacuously(
    tmp_path: Path,
) -> None:
    lane = InMemoryLane(run_tokens=0, run_cost_usd=Decimal(0), run_saved_usd=Decimal(0))
    handler, _, _ = build(tmp_path, lane)
    result = handler.run()
    assert "-organic_run_has_values" in result.assertions
    assert result.conclusion is EnumScenarioConclusion.FAILURE


def test_error_chain_a_totals_response_without_a_declared_key_is_a_failure_not_zero(
    tmp_path: Path,
) -> None:
    handler, rows, _ = build(
        tmp_path, http_lane(FakeTenantApi(drop_key="total_tokens"))
    )
    result = handler.run()
    assert result.assertions[-1] == "-totals_read_before"
    assert "total_tokens" in result.detail
    assert len(rows.rows) == 1


def test_error_chain_a_run_row_with_no_traffic_class_is_a_failure_not_organic(
    tmp_path: Path,
) -> None:
    api = FakeTenantApi()
    plain_get = api.get

    def without_label(url: str, headers: Mapping[str, str], timeout: float) -> object:
        body = plain_get(url, headers, timeout)
        if isinstance(body, dict) and "runs" in body:
            for item in body["runs"]:
                item.pop("traffic_class")
        return body

    lane = HttpLane(
        SETTINGS,
        {
            "R8_PROBE_API_BASE_URL": "https://api.example",
            "R8_PROBE_TENANT_API_KEY": "probe-key",
        },
        http_get=without_label,
        run_command=api.run,
    )
    handler, _, _ = build(tmp_path, lane)
    result = handler.run()
    assert result.assertions[-1] == "-runs_read"
    assert "traffic_class" in result.detail


def test_error_chain_a_delegate_command_that_names_no_correlation_id_is_a_failure(
    tmp_path: Path,
) -> None:
    api = FakeTenantApi()

    def refused(argv: Sequence[str], timeout: float) -> tuple[int, str]:
        assert timeout > 0
        assert argv
        return 2, "refused: synthetic needs a source surface"

    lane = HttpLane(
        SETTINGS,
        {
            "R8_PROBE_API_BASE_URL": "https://api.example",
            "R8_PROBE_TENANT_API_KEY": "probe-key",
        },
        http_get=api.get,
        run_command=refused,
    )
    handler, _, _ = build(tmp_path, lane)
    result = handler.run()
    assert result.assertions[-1] == "-runs_sent"
    assert "printed no JSON" in result.detail


@pytest.mark.parametrize(
    "missing", ["R8_PROBE_API_BASE_URL", "R8_PROBE_TENANT_API_KEY"]
)
def test_error_chain_a_lane_with_no_address_or_no_key_is_not_configured(
    missing: str,
) -> None:
    env = {
        "R8_PROBE_API_BASE_URL": "https://api.example",
        "R8_PROBE_TENANT_API_KEY": "k",
    }
    del env[missing]
    with pytest.raises(LaneReadError, match=missing):
        HttpLane(SETTINGS, env)


def test_error_chain_unreadable_inputs_is_a_failure_and_still_writes_one_row(
    tmp_path: Path,
) -> None:
    handler, rows, events = build(
        tmp_path, InMemoryLane(), inputs_path=tmp_path / "missing.yaml"
    )
    result = handler.run()
    assert result.assertions == ("-inputs_readable",)
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert len(rows.rows) == 1
    assert len(events.events) == 1


def test_error_chain_a_ledger_append_that_fails_raises_and_emits_nothing(
    tmp_path: Path,
) -> None:
    def refuse(row: str) -> None:
        raise RuntimeError("the ledger append was rejected")

    events = InMemoryScenarioEventSink()
    handler = HandlerScenarioR8(
        RUNTIME,
        home=tmp_path,
        environment={},
        appender=refuse,
        sink=events,
        terminal_topic=TERMINAL,
        lane_factory=InMemoryLane,
        clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="ledger append"):
        handler.run()
    assert events.events == []


def test_error_chain_a_bus_emit_that_fails_raises_after_the_row_is_written(
    tmp_path: Path,
) -> None:
    class Down:
        def emit(self, topic: str, payload: Mapping[str, object]) -> None:
            raise RuntimeError("the bus emit failed")

    rows = Rows()
    handler = HandlerScenarioR8(
        RUNTIME,
        home=tmp_path,
        environment={},
        appender=rows,
        sink=Down(),
        terminal_topic=TERMINAL,
        lane_factory=InMemoryLane,
        clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="bus emit"):
        handler.run()
    assert len(rows.rows) == 1


def test_the_schedule_block_renders_a_six_hourly_user_timer() -> None:
    schedule = load_schedule(CONTRACT_PATH)
    service, timer = render_systemd(
        schedule,
        node_module="omnimarket.nodes.node_delegation_scenario_r8_effect",
        omni_root=Path("/opt/omni"),
        home=Path("/home/lab"),
        python=Path("/opt/omni/omnimarket/.venv/bin/python"),
        environment={"ONEX_HOST_NAME": "lab-a"},
    )
    assert "OnCalendar=*-*-* 00,06,12,18:50:00 UTC" in timer
    assert "-m omnimarket.nodes.node_delegation_scenario_r8_effect run" in service


def test_the_models_refuse_unknown_keys() -> None:
    with pytest.raises(ValueError, match="surprise"):
        ModelTotals.model_validate(
            {"runs": 1, "tokens": 1, "spend_usd": 0, "savings_usd": 0, "surprise": 1}
        )
    with pytest.raises(ValueError, match="surprise"):
        ModelRunRow.model_validate(
            {
                "correlation_id": "a",
                "traffic_class": "organic",
                "tokens": 1,
                "cost_usd": 0,
                "saved_usd": 0,
                "surprise": 1,
            }
        )
    with pytest.raises(ValueError, match="surprise"):
        ModelLaneSettings.model_validate({"surprise": 1})
    assert NODE_DIR.name == "node_delegation_scenario_r8_effect"
