# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the silent-failure alarm nodes, including quiet-host capture repair."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from omnimarket.models.silent_failure_alarm import (
    ModelDrainerObservation,
    ModelDrainerRepair,
    ModelHostActivity,
    ModelQuietHostRequest,
)
from omnimarket.nodes.node_silent_failure_alarm_compute.handlers import (
    HandlerPlacementShareAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_compute.models import (
    ModelLaneRefusal,
    ModelPlacementRun,
    ModelPlacementShareRequest,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.quiet_host_repair import (
    QuietHostRepairPlanner,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 9, 6, 0, tzinfo=UTC)


def _run(key: str, dispatched: int, placed: int, minutes_ago: int) -> ModelPlacementRun:
    return ModelPlacementRun(
        run_key=key,
        stamp=NOW - timedelta(minutes=minutes_ago),
        dispatched=dispatched,
        placed=placed,
    )


def test_golden_chain_two_runs_under_half_alarm_with_friction_cells() -> None:
    request = ModelPlacementShareRequest(
        now=NOW,
        runs=(_run("a", 4, 0, 80), _run("b", 4, 1, 60)),
        refusals=tuple(
            ModelLaneRefusal(run_key="b", lane=f"lane{i}", status="repo-unresolved")
            for i in range(3)
        ),
    )
    result = HandlerPlacementShareAlarm().handle(request)
    assert result.alarm
    assert result.reason == "repo-unresolved"
    assert "FRICTION" in result.friction_cells
    assert result.alert_text


def test_golden_chain_a_healthy_run_carries_no_rows() -> None:
    request = ModelPlacementShareRequest(now=NOW, runs=(_run("a", 3, 3, 20),))
    result = HandlerPlacementShareAlarm().handle(request)
    assert not result.alarm
    assert result.friction_cells == ""
    assert result.alert_text == ""


def test_error_chain_a_malformed_request_never_reaches_the_handler() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelPlacementShareRequest.model_validate({"now": "not a time", "runs": "none"})
    assert {err["loc"][0] for err in caught.value.errors()} >= {"now", "runs"}


HOST_NOW = datetime(2026, 11, 1, 15, 33, 25, tzinfo=UTC)
RECORDED = {"h051": 356, "h053": 37, "h101": 357, "h105": 357, "h106": 356}


class Watch:
    def __init__(self, observation: ModelDrainerObservation) -> None:
        self.observation = observation
        self.repairs = 0

    def observe(self) -> ModelDrainerObservation:
        return self.observation

    def repair(self) -> ModelDrainerRepair:
        self.repairs += 1
        return ModelDrainerRepair(
            succeeded=True,
            after=ModelDrainerObservation(
                job_state="running",
                interpreter_present=True,
                last_cycle_at=HOST_NOW,
            ),
        )


def snapshot() -> ModelQuietHostRequest:
    return ModelQuietHostRequest(
        now=HOST_NOW,
        hosts=tuple(
            ModelHostActivity(
                host=host, lane_stamps=(HOST_NOW - timedelta(minutes=minutes), HOST_NOW)
            )
            for host, minutes in RECORDED.items()
        ),
    )


def test_recorded_hosts_each_get_safe_repair_but_installer_success_keeps_alarm() -> (
    None
):
    watches = {
        host: Watch(
            ModelDrainerObservation(job_state="not-running", status_readable=False)
        )
        for host in RECORDED
    }
    handler = QuietHostRepairPlanner(watches=watches, read_events=lambda: ({}, ""))
    result = handler.handle(snapshot(), apply=True)
    assert result.decision.state == "QUIET"
    assert result.decision.quiet_hosts == tuple(sorted(RECORDED))
    assert all(watch.repairs == 1 for watch in watches.values())
    assert {r.state for r in result.repairs} == {"pending-events"}
    assert "class=quiet-host" in result.decision.friction_cells


def test_cycling_broker_failure_and_unknown_supervisor_are_never_restarted() -> None:
    watches = {
        host: Watch(
            ModelDrainerObservation(
                job_state="running",
                last_cycle_at=HOST_NOW,
                last_publish_at=HOST_NOW - timedelta(hours=2),
                journal_depth=7,
            )
        )
        for host in RECORDED
    }
    watches["h053"] = Watch(
        ModelDrainerObservation(job_state="unknown", status_readable=False)
    )
    handler = QuietHostRepairPlanner(watches=watches, read_events=lambda: ({}, ""))
    result = handler.handle(snapshot(), apply=True)
    assert result.decision.alarm
    assert all(watch.repairs == 0 for watch in watches.values())


def test_dry_run_reads_but_never_repairs() -> None:
    watch = Watch(
        ModelDrainerObservation(job_state="not-running", status_readable=False)
    )
    result = QuietHostRepairPlanner(
        watches={"h053": watch}, read_events=lambda: ({}, "")
    ).handle(snapshot(), apply=False)
    assert watch.repairs == 0
    assert result.decision.alarm


def test_october_recorded_hosts_remain_quiet() -> None:
    request = ModelQuietHostRequest(
        now=HOST_NOW,
        hosts=(
            ModelHostActivity(
                host="h101", lane_stamps=(HOST_NOW - timedelta(minutes=272), HOST_NOW)
            ),
            ModelHostActivity(
                host="h202", lane_stamps=(HOST_NOW - timedelta(minutes=352), HOST_NOW)
            ),
        ),
    )
    result = QuietHostRepairPlanner(watches={}, read_events=lambda: ({}, "")).handle(
        request, apply=True
    )
    assert result.decision.quiet_hosts == ("h101", "h202")
    assert "4h32m" in result.decision.findings[0]
    assert "5h52m" in result.decision.findings[1]
    assert {r.state for r in result.repairs} == {"unconfigured"}


def test_only_the_host_with_a_new_post_attempt_event_records_recovery() -> None:
    watches = {
        host: Watch(
            ModelDrainerObservation(job_state="not-running", status_readable=False)
        )
        for host in RECORDED
    }
    calls = iter([HOST_NOW] * len(RECORDED) + [HOST_NOW + timedelta(seconds=5)])
    events = {
        "h051": HOST_NOW + timedelta(seconds=1),
        "h053": HOST_NOW,
        "h101": HOST_NOW - timedelta(hours=1),
        "h105": HOST_NOW + timedelta(minutes=1),
    }
    result = QuietHostRepairPlanner(
        watches=watches, read_events=lambda: (events, ""), clock=lambda: next(calls)
    ).handle(snapshot(), apply=True)
    assert [r.host for r in result.repairs if r.state == "recovered"] == ["h051"]
    assert result.decision.alarm
    assert len(result.decision.quiet_hosts) == 5
    assert result.repairs[0].event_at == events["h051"]


def test_unreadable_event_source_never_probes_or_repairs() -> None:
    watch = Watch(
        ModelDrainerObservation(job_state="not-running", status_readable=False)
    )
    request = snapshot().model_copy(update={"events_readable": False})
    result = QuietHostRepairPlanner(
        watches={"h053": watch}, read_events=lambda: ({}, "")
    ).handle(request, apply=True)
    assert result.decision.state == "UNKNOWN"
    assert result.repairs == ()
    assert watch.repairs == 0


def test_readback_failure_preserves_pending_events_and_alarm() -> None:
    watch = Watch(
        ModelDrainerObservation(job_state="not-running", status_readable=False)
    )
    result = QuietHostRepairPlanner(
        watches={"h053": watch}, read_events=lambda: ({}, "database unreadable")
    ).handle(snapshot(), apply=True)
    assert result.decision.alarm
    assert next(r.state for r in result.repairs if r.host == "h053") == "pending-events"
    assert result.warnings == (
        "capture repair readback unreadable: database unreadable",
    )


def test_live_drainer_with_missing_capture_is_left_alone() -> None:
    watch = Watch(
        ModelDrainerObservation(
            job_state="running", last_cycle_at=HOST_NOW, last_publish_at=HOST_NOW
        )
    )
    result = QuietHostRepairPlanner(
        watches={"h053": watch}, read_events=lambda: ({}, "")
    ).handle(snapshot(), apply=True)
    assert watch.repairs == 0
    assert result.decision.alarm
    assert next(r.state for r in result.repairs if r.host == "h053") == "not-needed"


def test_cooldown_survives_a_new_handler_and_failed_install(tmp_path) -> None:
    class FailedWatch(Watch):
        def repair(self) -> ModelDrainerRepair:
            self.repairs += 1
            return ModelDrainerRepair(succeeded=False, after=self.observation)

    watch = FailedWatch(
        ModelDrainerObservation(job_state="not-running", status_readable=False)
    )
    path = tmp_path / "state/repairs.json"

    def tick():
        return QuietHostRepairPlanner(
            watches={"h053": watch},
            read_events=lambda: ({}, ""),
            state_file=path,
            clock=lambda: HOST_NOW,
        ).handle(snapshot(), apply=True)

    first = tick()
    second = tick()
    assert watch.repairs == 1
    assert next(r.state for r in first.repairs if r.host == "h053") == "failed"
    assert next(r.state for r in second.repairs if r.host == "h053") == "cooldown"
    assert first.decision.alarm
    assert second.decision.alarm


def test_corrupt_state_does_not_restart_hosts(tmp_path) -> None:
    path = tmp_path / "repairs.json"
    path.write_text("not-json")
    watch = Watch(
        ModelDrainerObservation(job_state="not-running", status_readable=False)
    )
    result = QuietHostRepairPlanner(
        watches={"h053": watch}, read_events=lambda: ({}, ""), state_file=path
    ).handle(snapshot(), apply=True)
    assert watch.repairs == 0
    assert result.decision.alarm
    assert "state unreadable" in result.warnings[0]


def test_repair_cap_is_per_tick_and_not_an_alarm_cap() -> None:
    watches = {
        host: Watch(
            ModelDrainerObservation(job_state="not-running", status_readable=False)
        )
        for host in RECORDED
    }
    result = QuietHostRepairPlanner(
        watches=watches, read_events=lambda: ({}, ""), max_repairs=2
    ).handle(snapshot(), apply=True)
    assert sum(w.repairs for w in watches.values()) == 2
    assert len(result.decision.quiet_hosts) == 5
    assert sum(r.state == "limited" for r in result.repairs) == 3


def world(tmp_path):
    import json
    import sys

    from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
        ModelSilentFailureAlarmOptions,
    )

    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "\n".join(
            f"{stamp.strftime('%Y-%m-%dT%H:%M:%SZ')} | CLAIM | lane=recorded | host={host} | recorded activity"
            for host, minutes in RECORDED.items()
            for stamp in (HOST_NOW - timedelta(minutes=minutes), HOST_NOW)
        )
        + "\n"
    )
    events = tmp_path / "events.txt"
    events.write_text("")
    controller = tmp_path / "controller"
    controller.mkdir()
    (controller / "ticks.jsonl").write_text(
        json.dumps({"ts": HOST_NOW.isoformat(), "mode": "observe", "status": "OK"})
    )
    trend = tmp_path / "trend.json"
    trend.write_text(
        json.dumps({"last": {"ts": HOST_NOW.strftime("%Y-%m-%dT%H:%M:%SZ")}})
    )
    watcher = tmp_path / "watcher.json"
    watcher.write_text(
        json.dumps(
            {
                "last_tick": HOST_NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "prs": {},
                "merges": {},
            }
        )
    )
    appender = tmp_path / "append.py"
    appender.write_text(
        "import sys\nwith open(sys.argv[1], 'a') as f: f.write(sys.argv[3]+'\\n')\n"
    )
    alerts = tmp_path / "alert.sh"
    alerts.write_text("alert_channel_send() { echo 1234567890.123; }\n")
    return ModelSilentFailureAlarmOptions(
        registry_root=tmp_path,
        ledger=ledger,
        ledger_lock=str(appender),
        trend_file=trend,
        watcher_state=watcher,
        controller_state_dir=controller,
        min_merges_per_hour=0,
        min_lanes_placed_per_hour=0,
        alert_lib=alerts,
        alert_env_file=tmp_path / "empty.env",
        job_state_file=tmp_path / "jobs.json",
        delivery_state_file=tmp_path / "deliveries.json",
        capture_repair_state_file=tmp_path / "capture.json",
        python=sys.executable,
        host_aliases={host: host for host in RECORDED},
        hook_events_command=(
            sys.executable,
            "-c",
            "import sys;sys.stdout.write(open(sys.argv[1]).read())",
            str(events),
        ),
    )


def test_migrated_tick_runs_recorded_rows_through_real_readers_and_keeps_alarm(
    tmp_path,
) -> None:
    from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_silent_failure_alarm import (
        HandlerSilentFailureAlarm,
    )
    from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
        ModelSilentFailureAlarmRequest,
    )

    options = world(tmp_path)
    watches = {
        host: Watch(
            ModelDrainerObservation(job_state="not-running", status_readable=False)
        )
        for host in RECORDED
    }
    result = HandlerSilentFailureAlarm(options, host_watches=watches).handle(
        ModelSilentFailureAlarmRequest(now=HOST_NOW)
    )
    assert result.exit_code == 0, result.warnings
    assert result.alarms == ("quiet-host",)
    assert all(watch.repairs == 1 for watch in watches.values())
    assert len(result.capture_repairs) == 5
    assert len(result.rows) == 1
    assert "hosts=h051,h053,h101,h105,h106" in result.rows[0]
    assert options.ledger.read_text().count("class=quiet-host") == 1


def test_remote_observer_cannot_claim_another_hosts_observation(tmp_path) -> None:
    import sys

    import pytest

    from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.command_drainer_watch import (
        CommandDrainerWatchProbe,
    )
    from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
        ModelSilentFailureAlarmRequest,
        ModelSilentFailureAlarmResult,
    )
    from omnimarket.nodes.node_silent_failure_alarm_effect.models.model_quiet_host_repair import (
        ModelHostDrainerCommand,
    )

    result = ModelSilentFailureAlarmResult(
        request_id=ModelSilentFailureAlarmRequest().request_id,
        host="h105",
        exit_code=0,
        summary="",
        observation=ModelDrainerObservation(job_state="not-running"),
    )
    script = tmp_path / "node.py"
    script.write_text(
        "import sys\nsys.stdout.write(" + repr(result.model_dump_json()) + ")\n"
    )
    watch = CommandDrainerWatchProbe(
        "h053", ModelHostDrainerCommand(command=(sys.executable, str(script)))
    )
    with pytest.raises(ValueError, match="another host"):
        watch.observe()


def test_drainer_installer_receives_backlog_and_the_observed_state_root(
    tmp_path,
) -> None:
    import subprocess

    from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.drainer_watch import (
        DrainerWatchProbe,
    )
    from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
        ModelDrainerWatchOptions,
    )

    installer = tmp_path / "install.sh"
    installer.write_text("#!/bin/bash\nexit 0\n")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[-1] == "--dry-run":
            return subprocess.CompletedProcess(
                argv,
                0,
                f"ExecStart=/python /drainer --preserve-backlog\nEnvironment=ONEX_STATE_DIR={tmp_path / 'state'}\n",
                "",
            )
        return subprocess.CompletedProcess(argv, 0, "active\n", "")

    options = ModelDrainerWatchOptions(
        host="h053",
        state_dir=tmp_path / "state",
        installer=installer,
        home=tmp_path,
        registry_root=tmp_path,
        supervisor="systemd",
        settle_seconds=0,
    )
    result = DrainerWatchProbe(options, run=run).repair()
    assert result.succeeded
    assert calls[0][0] == ["bash", str(installer), "--dry-run"]
    argv, kwargs = calls[1]
    assert argv == ["bash", str(installer)]
    assert kwargs["env"]["ONEX_HOOK_EMIT_PRESERVE_BACKLOG"] == "1"
    assert kwargs["env"]["ONEX_STATE_DIR"] == str(options.state_dir)
    assert kwargs["cwd"] == tmp_path


def test_runtime_cli_uses_the_declared_bus_contract(
    tmp_path, monkeypatch, capsys
) -> None:
    import json

    from omnimarket.nodes.node_silent_failure_alarm_effect.runtime_silent_failure_alarm import (
        main,
    )

    options = world(tmp_path)
    config = tmp_path / "config.json"
    config.write_text(options.model_dump_json())
    assert (
        main(
            [
                "tick",
                "--config",
                str(config),
                "--now",
                HOST_NOW.isoformat(),
                "--dry-run",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["alarms"] == ["quiet-host"]
    assert len(result["capture_repairs"]) == 5


def test_repair_state_is_durable_before_the_installer_runs(tmp_path) -> None:
    import json

    path = tmp_path / "repairs.json"

    class InspectWatch(Watch):
        def repair(self) -> ModelDrainerRepair:
            assert json.loads(path.read_text())["h053"][
                "attempted_at"
            ] == HOST_NOW.isoformat().replace("+00:00", "Z")
            return super().repair()

    watch = InspectWatch(
        ModelDrainerObservation(job_state="not-running", status_readable=False)
    )
    QuietHostRepairPlanner(
        watches={"h053": watch},
        read_events=lambda: ({}, ""),
        state_file=path,
        clock=lambda: HOST_NOW,
    ).handle(snapshot(), apply=True)
    assert watch.repairs == 1


def test_a_second_repair_tick_cannot_restart_the_same_host(tmp_path) -> None:
    import fcntl

    path = tmp_path / "repairs.json"
    watch = Watch(
        ModelDrainerObservation(job_state="not-running", status_readable=False)
    )
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = QuietHostRepairPlanner(
            watches={"h053": watch}, read_events=lambda: ({}, ""), state_file=path
        ).handle(snapshot(), apply=True)
    assert watch.repairs == 0
    assert result.decision.alarm
    assert result.warnings == ("capture repair locked by another tick",)


def test_unwritable_attempt_record_prevents_install(tmp_path, monkeypatch) -> None:
    watch = Watch(
        ModelDrainerObservation(job_state="not-running", status_readable=False)
    )
    handler = QuietHostRepairPlanner(
        watches={"h053": watch},
        read_events=lambda: ({}, ""),
        state_file=tmp_path / "repairs.json",
    )

    def refused():
        raise OSError("read-only state")

    monkeypatch.setattr(handler, "_save", refused)
    result = handler.handle(snapshot(), apply=True)
    assert watch.repairs == 0
    assert result.decision.alarm
    assert "read-only state" in result.warnings[0]


def test_disabled_host_is_observed_by_alarm_but_never_repaired(tmp_path) -> None:
    from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_silent_failure_alarm import (
        HandlerSilentFailureAlarm,
    )
    from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
        ModelSilentFailureAlarmRequest,
    )

    options = world(tmp_path).model_copy(update={"repair_disabled_hosts": ("h053",)})
    watch = Watch(
        ModelDrainerObservation(job_state="not-running", status_readable=False)
    )
    result = HandlerSilentFailureAlarm(options, host_watches={"h053": watch}).handle(
        ModelSilentFailureAlarmRequest(now=HOST_NOW)
    )
    assert result.alarms == ("quiet-host",)
    assert watch.repairs == 0


def test_migrated_watcher_bar_remains_eight_minutes() -> None:
    from omnimarket.models.silent_failure_alarm import ModelTickLivenessRequest
    from omnimarket.nodes.node_silent_failure_alarm_compute import (
        HandlerTickLivenessAlarm,
    )

    verdict = HandlerTickLivenessAlarm().handle(
        ModelTickLivenessRequest(
            now=HOST_NOW,
            trend_at=HOST_NOW,
            controller_live=True,
            watcher_read=True,
            watcher_at=HOST_NOW - timedelta(minutes=9),
        )
    )
    assert verdict.alarm
    assert "PR watcher snapshot" in verdict.stale[0]


async def test_installed_node_discovery_and_bus_dispatch(tmp_path, monkeypatch) -> None:
    import asyncio
    import json
    from pathlib import Path
    from uuid import uuid4

    import yaml
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    from omnimarket.nodes.node_silent_failure_alarm_effect import node

    options = world(tmp_path)
    config = tmp_path / "config.json"
    config.write_text(options.model_dump_json())
    monkeypatch.setenv("ONEX_SILENT_FAILURE_ALARM_CONFIG", str(config))
    contracts = tuple(
        c
        for c in discover_contracts().contracts
        if c.name == "node_silent_failure_alarm_effect"
    )
    assert len(contracts) == 1
    raw = yaml.safe_load(Path(node.__file__).with_name("contract.yaml").read_text())
    completed = asyncio.Queue()
    bus = EventBusInmemory(environment="local", group="capture-repair")
    await bus.start()

    async def collect(message):
        await completed.put(json.loads(message.value))

    try:
        await bus.subscribe(
            raw["terminal_event"], group_id="capture-results", on_message=collect
        )
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=contracts),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert [r.outcome.value for r in report.results] == ["wired"]
        assert set(report.results[0].topics_subscribed) == set(
            raw["event_bus"]["subscribe_topics"]
        )
        engine.freeze()
        correlation = uuid4()
        envelope = ModelEventEnvelope[object](
            payload={"operation": "tick", "apply": False, "now": HOST_NOW.isoformat()},
            correlation_id=correlation,
            event_type="omnimarket.silent-failure-alarm-requested",
        )
        await bus.publish(
            raw["runtime_dispatch"]["command_topic"],
            None,
            envelope.model_dump_json().encode(),
            None,
        )
        event = await asyncio.wait_for(completed.get(), timeout=3)
        assert event["correlation_id"] == str(correlation)
        assert (
            raw["terminal_event"]
            == "onex.evt.omnimarket.silent-failure-alarm-completed.v1"
        )
        assert event["payload"]["alarms"] == ["quiet-host"]
        assert len(event["payload"]["capture_repairs"]) == 5
    finally:
        await bus.close()


def test_registry_installer_points_alarm_unit_at_the_clean_branch(
    tmp_path, monkeypatch
) -> None:
    import json
    import plistlib
    import subprocess

    from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
        HandlerRegistryUnits,
    )
    from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
        ModelRegistryUnitsRequest,
    )

    branch = tmp_path / "branch"
    entry = (
        branch
        / "src/omnimarket/nodes/node_silent_failure_alarm_effect/runtime_silent_failure_alarm.py"
    )
    entry.parent.mkdir(parents=True)
    entry.write_text("# branch entry\n")
    revision = "a" * 40

    def run(argv):
        if argv[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(argv, 0, revision + "\n", "")
        if argv[-1] == "-u":
            return subprocess.CompletedProcess(argv, 0, "501\n", "")
        if argv[-1] == "-un":
            return subprocess.CompletedProcess(argv, 0, "fixture-user\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda _path: True,
    )
    request = ModelRegistryUnitsRequest(
        unit="silent-failure-alarm",
        mode="install",
        home=str(tmp_path / "home"),
        registry_root=str(tmp_path / "registry"),
        workspace_config_root=str(tmp_path / "overlay"),
        project_path=str(branch),
        expected_revision=revision,
        path_overrides={
            "ONEX_SILENT_FAILURE_ALARM_CONFIG": str(tmp_path / "alarm-config.json")
        },
    )
    result = HandlerRegistryUnits(run=run).handle(request)
    assert result.exit_code == 0, result.stderr
    assert result.revision == revision
    assert result.project == str(branch)
    plist = plistlib.loads(result.rendered.encode())
    assert str(branch) in plist["ProgramArguments"]
    assert (
        "omnimarket.nodes.node_silent_failure_alarm_effect.runtime_silent_failure_alarm"
        in plist["ProgramArguments"]
    )
    assert plist["Label"] == "ai.omninode.silent-failure-alarm"
    assert plist["EnvironmentVariables"]["ONEX_SILENT_FAILURE_ALARM_CONFIG"] == str(
        tmp_path / "alarm-config.json"
    )
    assert (
        json.loads(__import__("pathlib").Path(result.record).read_text())["revision"]
        == revision
    )


def test_legacy_linux_installer_without_preserve_flag_is_never_activated(
    tmp_path,
) -> None:
    import subprocess

    from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.drainer_watch import (
        DrainerWatchProbe,
    )
    from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
        ModelDrainerWatchOptions,
    )

    installer = tmp_path / "install.sh"
    installer.write_text("#!/bin/bash\nexit 0\n")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            "ExecStart=/python /drainer\nEnvironment=ONEX_STATE_DIR=/state\n",
            "",
        )

    options = ModelDrainerWatchOptions(
        host="h053",
        state_dir=tmp_path / "state",
        installer=installer,
        home=tmp_path,
        registry_root=tmp_path,
        supervisor="systemd",
        settle_seconds=0,
    )
    result = DrainerWatchProbe(options, run=run).repair()
    assert not result.succeeded
    assert "activation refused" in result.detail
    assert ["bash", str(installer), "--dry-run"] in calls
    assert ["bash", str(installer)] not in calls
