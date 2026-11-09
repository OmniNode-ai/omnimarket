# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of scenario R2 (OMN-23172): one run writes one STATUS row and emits one result on the bus.

The golden chain runs the scenario against a scripted ``onex`` that behaves as the product does. The error chains are
the named injected breaks: each makes exactly one product behaviour wrong and shows the scenario red for that reason.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.delegation_scenario_runtime import (
    InMemoryScenarioEventSink,
    render_systemd,
)
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    ModelScenarioR2RunCommand,
    from_status_row,
)
from omnimarket.nodes.node_delegation_scenario_r2_effect.handlers.handler_scenario_r2 import (
    HandlerScenarioR2,
    ModelCommandOutcome,
    build_runner,
    load_runtime,
    load_schedule,
)

pytestmark = pytest.mark.unit

TERMINAL = "onex.evt.omnimarket.delegation-scenario-result.v1"
NOW = datetime(2026, 11, 9, 13, 6, 30, tzinfo=UTC)
RUNTIME = load_runtime()
KEY = "sk-the-customer-key-value"
SETTINGS = {
    RUNTIME.onboarding.provider_env: "openai_compatible",
    RUNTIME.onboarding.provider_url_env: "http://provider.invalid/v1",
    RUNTIME.onboarding.key_env: KEY,
    RUNTIME.onboarding.model_env: "provider-model",
    RUNTIME.local_first_env: "http://lab-a.invalid:8000/v1|model-a",
    RUNTIME.local_second_env: "http://lab-b.invalid:8000/v1|model-b",
    RUNTIME.dead_url_env: "http://unreachable.invalid",
}


class ScriptedOnex:
    """A stand-in for the onex command with the product's behaviour, and one named wrong behaviour when asked."""

    def __init__(self, brk: str | None = None) -> None:
        self.brk = brk
        self.provider: str | None = None
        self.local: list[str] = []
        self.calls: list[list[str]] = []
        self.stdin: list[str | None] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None,
        home: Path,
        timeout_seconds: int,
    ) -> ModelCommandOutcome:
        args = list(argv)[1:]
        self.calls.append(args)
        self.stdin.append(stdin)
        assert home.is_dir()
        if args[:2] == ["models", "list"]:
            rows = [
                {
                    "provider": "openai_compatible",
                    "set_up": self.provider is not None,
                    "models": ["provider-model"] if self.provider else [],
                },
                {
                    "provider": "local",
                    "set_up": bool(self.local),
                    "models": list(self.local),
                },
            ]
            return ModelCommandOutcome(
                returncode=0, stdout="\n".join(json.dumps(r) for r in rows)
            )
        if args[:2] == ["models", "add"]:
            if args[2] == "local":
                url, model = (
                    args[args.index("--url") + 1],
                    args[args.index("--model") + 1],
                )
                if (
                    url == SETTINGS[RUNTIME.dead_url_env]
                    and self.brk != "dead_url_accepted"
                ):
                    return ModelCommandOutcome(
                        returncode=1, stderr="the server did not answer"
                    )
                if self.brk == "second_add_replaces_first" and self.local:
                    self.local = [model]
                else:
                    self.local.append(model)
                return ModelCommandOutcome(returncode=0, stdout="{}")
            if self.brk == "onboarding_fails":
                return ModelCommandOutcome(returncode=1, stderr="key refused")
            self.provider = args[2]
            return ModelCommandOutcome(returncode=0, stdout="{}")
        if args[0] == "delegate":
            backend = "cloud" if self.brk == "delegation_leaves_local" else "local-url"
            attempt = {
                "backend_id": backend,
                "model_id": self.local[0],
                "acceptance_decision": "accept",
            }
            receipt = {
                "result": {"terminal_payload": {"payload": {"attempts": [attempt]}}}
            }
            return ModelCommandOutcome(
                returncode=0, stdout="transport line\n" + json.dumps(receipt)
            )
        return ModelCommandOutcome(returncode=2, stderr="unknown command")


def _run(
    tmp_path: Path,
    onex: ScriptedOnex,
    *,
    settings: dict[str, str] | None = None,
    trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
) -> tuple[HandlerScenarioR2, list[str], InMemoryScenarioEventSink]:
    rows: list[str] = []
    sink = InMemoryScenarioEventSink()
    handler = HandlerScenarioR2(
        RUNTIME,
        home=tmp_path,
        environment=SETTINGS if settings is None else settings,
        runner=onex,
        appender=rows.append,
        sink=sink,
        trigger=trigger,
        clock=lambda: NOW,
    )
    return handler, rows, sink


def test_golden_chain_scenario_r2_declares_its_schedule_and_topic() -> None:
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_delegation_scenario_r2_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    assert contract["name"] == "node_delegation_scenario_r2_effect"
    assert contract["terminal_event"] == TERMINAL
    assert contract["externally_consumed_topics"] == [TERMINAL]
    assert contract["runtime"]["cadence"] == "hourly"
    schedule = load_schedule()
    assert schedule.calendar == "*-*-* *:05:00 UTC"
    service, timer = render_systemd(
        schedule,
        node_module="omnimarket.nodes.node_delegation_scenario_r2_effect",
        omni_root=Path("/srv/registry"),
        home=Path("/srv/user"),
        python=Path("/usr/bin/python3"),
        environment={"ONEX_HOST_NAME": "lab-host"},
    )
    assert (
        "ExecStart=/usr/bin/python3 -m omnimarket.nodes.node_delegation_scenario_r2_effect run\n"
        in service
    )
    assert "EnvironmentFile=-/srv/user/.config/onex/scenario-r2.env\n" in service
    assert 'Environment="ONEX_HOST_NAME=lab-host"' in service
    assert "OnCalendar=*-*-* *:05:00 UTC\n" in timer
    assert "Persistent=false\n" in timer


def test_golden_chain_scenario_r2_passes_writes_one_row_and_emits_one_event(
    tmp_path: Path,
) -> None:
    onex = ScriptedOnex()
    handler, rows, sink = _run(tmp_path, onex)
    result = handler.handle(ModelScenarioR2RunCommand())
    assert result.conclusion is EnumScenarioConclusion.SUCCESS
    assert result.assertions == (
        "+settings_present",
        "+fresh_home",
        "+onboarded",
        "+dead_url_refused",
        "+dead_url_wrote_no_route",
        "+local_added",
        "+list_names_both",
        "+delegated_to_local_url",
        "+both_listed_in_order",
    )
    assert result.slot_start == datetime(2026, 11, 9, 13, 0, tzinfo=UTC)
    assert len(rows) == 1
    assert (
        " | STATUS | lane=node_delegation_scenario_r2_effect | ticket=OMN-23172 | "
        in rows[0]
    )
    assert (
        "scenario=R2 | slot=2026-11-09T13:00:00Z | trigger=schedule | conclusion=success"
        in rows[0]
    )
    parsed = from_status_row(rows[0])
    assert parsed is not None
    assert parsed.conclusion is EnumScenarioConclusion.SUCCESS
    assert [topic for topic, _ in sink.events] == [TERMINAL]
    assert sink.events[0][1]["conclusion"] == "success"


def test_golden_chain_scenario_r2_keeps_the_key_on_stdin_only(tmp_path: Path) -> None:
    onex = ScriptedOnex()
    handler, rows, sink = _run(tmp_path, onex)
    handler.run()
    assert onex.stdin.count(KEY + "\n") == 1
    assert all(KEY not in " ".join(call) for call in onex.calls)
    assert [call[0] for call in onex.calls if call[0] == "delegate"] == ["delegate"]
    delegate = next(call for call in onex.calls if call[0] == "delegate")
    assert delegate[delegate.index("--bus") : delegate.index("--bus") + 4] == [
        "--bus",
        "inmemory",
        "--locus",
        "in-process",
    ]
    assert KEY not in rows[0]
    assert KEY not in json.dumps(sink.events)


def test_golden_chain_scenario_r2_starts_from_a_fresh_home_every_run(
    tmp_path: Path,
) -> None:
    handler, _, _ = _run(tmp_path, ScriptedOnex())
    stale = handler.work_home / "left-over"
    stale.parent.mkdir(parents=True)
    stale.write_text("x", encoding="utf-8")
    handler.run()
    assert not stale.exists()


def test_golden_chain_scenario_r2_a_manual_run_is_marked_manual(tmp_path: Path) -> None:
    handler, rows, _ = _run(
        tmp_path, ScriptedOnex(), trigger=EnumScenarioTrigger.MANUAL
    )
    handler.run()
    assert "trigger=manual" in rows[0]


@pytest.mark.parametrize(
    ("brk", "failed"),
    [
        ("second_add_replaces_first", "-both_listed_in_order"),
        ("dead_url_accepted", "-dead_url_refused"),
        ("delegation_leaves_local", "-delegated_to_local_url"),
        ("onboarding_fails", "-onboarded"),
    ],
)
def test_error_chain_scenario_r2_names_the_break_it_catches(
    tmp_path: Path, brk: str, failed: str
) -> None:
    handler, rows, sink = _run(tmp_path, ScriptedOnex(brk))
    result = handler.run()
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert failed in result.assertions
    assert "conclusion=failure" in rows[0]
    assert failed.lstrip("-") in rows[0]
    assert sink.events[0][1]["conclusion"] == "failure"
    # The same scripted product without the break is green: the break is what turned it red.
    clean, _, _ = _run(tmp_path / "clean", ScriptedOnex())
    assert clean.run().conclusion is EnumScenarioConclusion.SUCCESS


def test_error_chain_scenario_r2_a_missing_setting_is_a_failure_that_runs_no_command(
    tmp_path: Path,
) -> None:
    onex = ScriptedOnex()
    settings = {k: v for k, v in SETTINGS.items() if k != RUNTIME.local_second_env}
    handler, rows, sink = _run(tmp_path, onex, settings=settings)
    result = handler.run()
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert result.assertions == ("-settings_present",)
    assert RUNTIME.local_second_env in result.detail
    assert onex.calls == []
    assert len(rows) == 1
    assert len(sink.events) == 1


def test_error_chain_scenario_r2_a_command_that_raises_is_a_failure_not_a_crash(
    tmp_path: Path,
) -> None:
    class Raising(ScriptedOnex):
        def run(
            self,
            argv: Sequence[str],
            *,
            stdin: str | None,
            home: Path,
            timeout_seconds: int,
        ) -> ModelCommandOutcome:
            raise OSError("no such command")

    handler, rows, _ = _run(tmp_path, Raising())
    result = handler.run()
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert "-unexpected_error" in result.assertions
    assert len(rows) == 1


def test_error_chain_scenario_r2_a_bus_emit_that_fails_raises_after_the_row_is_written(
    tmp_path: Path,
) -> None:
    class BrokenSink:
        def emit(self, topic: str, payload: Mapping[str, object]) -> None:
            raise RuntimeError("the bus emit failed")

    rows: list[str] = []
    handler = HandlerScenarioR2(
        RUNTIME,
        home=tmp_path,
        environment=SETTINGS,
        runner=ScriptedOnex(),
        appender=rows.append,
        sink=BrokenSink(),
        clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="bus emit"):
        handler.run()
    assert len(rows) == 1


def test_error_chain_scenario_r2_with_no_ledger_route_refuses_to_build(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match=RUNTIME.ledger_append_command_env):
        build_runner({RUNTIME.home_env: str(tmp_path)})


def test_golden_chain_scenario_r2_runs_through_the_bus_commands_the_environment_names(
    tmp_path: Path,
) -> None:
    script = tmp_path / "fake_bus.py"
    script.write_text(
        "import json, sys\n"
        "if sys.argv[1] == 'append':\n"
        "    open(sys.argv[2], 'a').write(sys.stdin.read())\n"
        "    print(json.dumps({'status': 'accepted', 'message': 'ok'}))\n"
        "else:\n"
        "    open(sys.argv[2], 'a').write(sys.argv[3] + '\\n' + sys.stdin.read())\n",
        encoding="utf-8",
    )
    appended, emitted = tmp_path / "appended.txt", tmp_path / "emitted.txt"
    runner = build_runner(
        {
            **SETTINGS,
            RUNTIME.home_env: str(tmp_path / "registry"),
            RUNTIME.ledger_append_command_env: f"{sys.executable} {script} append {appended}",
            RUNTIME.bus_emit_command_env: f"{sys.executable} {script} emit {emitted}",
            RUNTIME.host_env: "lab-host",
            "PATH": "/nonexistent",
        }
    )
    result = runner.run()
    # No onex on the path: every assertion after the settings is red, and the result still reached both routes.
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert (
        " | STATUS | lane=node_delegation_scenario_r2_effect | ticket=OMN-23172 | "
        in appended.read_text(encoding="utf-8")
    )
    assert emitted.read_text(encoding="utf-8").startswith(TERMINAL)
    journal = tmp_path / "registry" / RUNTIME.event_journal_relpath
    assert (
        json.loads(journal.read_text(encoding="utf-8").splitlines()[0])["topic"]
        == TERMINAL
    )
