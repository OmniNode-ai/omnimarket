# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay complete-inventory outages without weakening the landing freshness bar."""

import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_silent_failure_alarm_compute.handlers.handler_tick_liveness_alarm import (
    HandlerTickLivenessAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_compute.models import (
    ModelTickLivenessRequest,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_watcher_recovery import (
    HandlerWatcherRecovery,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.models.model_watcher_recovery import (
    ModelWatcherRecoveryOptions,
    ModelWatcherRecoveryRequest,
)

pytestmark = pytest.mark.unit

# Recorded alarm times and last complete inventory times, including the one-second overrun.
RECORDED = (
    ("2026-10-28T12:16:48Z", "2026-10-28T12:00:44Z"),
    ("2026-10-31T11:25:04Z", "2026-10-31T11:09:58Z"),
    ("2026-10-31T13:25:15Z", "2026-10-31T12:49:06Z"),
    ("2026-11-01T23:37:41Z", "2026-11-01T23:29:40Z"),
)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@pytest.mark.parametrize(("now", "last_complete"), RECORDED)
def test_recorded_rows_alarm_even_when_other_ticks_return(
    now: str, last_complete: str
) -> None:
    result = HandlerTickLivenessAlarm().handle(
        ModelTickLivenessRequest(
            now=at(now),
            trend_at=at(now),
            controller_live=True,
            watcher_read=True,
            watcher_at=at(last_complete),
        )
    )
    assert result.alarm
    assert result.state == "STALE"
    assert "PR watcher snapshot" in result.stale[0]


NOW = at("2026-10-31T13:25:15Z")


class Watcher:
    def __init__(self, root: Path):
        self.snapshot = root / "state.json"
        self.log = root / "watcher.log"
        self.record = root / "recovery.json"
        self.last = "2026-10-31T12:49:06Z"
        self.snapshot.write_text(
            json.dumps({"last_tick": self.last, "prs": {}, "merges": {}})
        )
        self.log.write_text("PR-INVENTORY STALE age=2169s\npr-watcher: tick ok\n")
        self.calls = []
        self.refresh = False
        self.exit = 0
        self.timeout = False
        self.options = ModelWatcherRecoveryOptions(
            watcher_state=self.snapshot,
            watcher_log=self.log,
            recovery_state=self.record,
            status_command=("watcher-adapter", "--status"),
            recovery_command=("watcher-adapter", "publish"),
        )

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if "--status" in argv:
            return subprocess.CompletedProcess(argv, 3, "PR-INVENTORY STALE", "")
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if self.refresh:
            self.snapshot.write_text(
                json.dumps(
                    {
                        "last_tick": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "prs": {},
                        "merges": {},
                    }
                )
            )
        return subprocess.CompletedProcess(argv, self.exit, "tick ok", "")

    def tick(self, now=NOW, apply=True):
        return HandlerWatcherRecovery(self.options, run=self.run).handle(
            ModelWatcherRecoveryRequest(now=now, apply=apply)
        )

    def recoveries(self):
        return [argv for argv, _ in self.calls if "publish" in argv]


@pytest.mark.parametrize(("now", "last_complete"), RECORDED)
def test_recorded_outages_diagnose_and_request_only_one_recovery(
    tmp_path, now, last_complete
):
    watcher = Watcher(tmp_path)
    watcher.snapshot.write_text(json.dumps({"last_tick": last_complete}))
    first = watcher.tick(at(now))
    assert first.outcome == "attempted"
    assert first.before_at == first.after_at == at(last_complete)
    assert "PR-INVENTORY STALE" in first.inventory_status
    assert "tick ok" in first.log_evidence[-1]
    assert watcher.tick(at(now) + timedelta(seconds=1)).outcome == "cooldown"
    assert len(watcher.recoveries()) == 1
    assert json.loads(watcher.record.read_text())["outcome"] == "attempted"
    assert all(call[1]["timeout"] <= 210 for call in watcher.calls)


def test_only_an_actual_complete_refresh_reports_recovered(tmp_path):
    watcher = Watcher(tmp_path)
    watcher.refresh = True
    result = watcher.tick()
    assert result.outcome == "recovered"
    assert result.after_at == NOW
    assert watcher.tick().outcome == "fresh"
    assert len(watcher.recoveries()) == 1


@pytest.mark.parametrize(("seconds", "alarm"), [(480, False), (481, True), (-1, True)])
def test_eight_minute_boundary_and_future_stamps(seconds, alarm):
    result = HandlerTickLivenessAlarm().handle(
        ModelTickLivenessRequest(
            now=NOW,
            trend_at=NOW,
            controller_live=True,
            watcher_read=True,
            watcher_at=NOW - timedelta(seconds=seconds),
        )
    )
    assert result.alarm is alarm


def test_cannot_widen_watcher_freshness():
    with pytest.raises(ValidationError):
        ModelTickLivenessRequest(now=NOW, watcher_max_age_minutes=9)


def test_missing_inventory_is_an_alarm(tmp_path):
    watcher = Watcher(tmp_path)
    watcher.snapshot.unlink()
    result = watcher.tick()
    assert result.after_at is None
    assert (
        HandlerTickLivenessAlarm()
        .handle(
            ModelTickLivenessRequest(
                now=NOW,
                trend_at=NOW,
                controller_live=True,
                watcher_read=True,
                watcher_at=result.after_at,
            )
        )
        .alarm
    )


def test_dry_run_reads_diagnostics_without_recovery_or_state_write(tmp_path):
    watcher = Watcher(tmp_path)
    assert watcher.tick(apply=False).outcome == "dry-run"
    assert not watcher.recoveries()
    assert not watcher.record.exists()
    assert not watcher.record.with_suffix(".lock").exists()


def test_a_second_concurrent_recovery_does_not_request_a_tick(tmp_path):
    watcher = Watcher(tmp_path)
    with watcher.record.with_suffix(".lock").open("a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert watcher.tick().outcome == "locked"
    assert not watcher.recoveries()


@pytest.mark.parametrize("mode", ["failed", "timeout"])
def test_failed_or_timed_out_recovery_keeps_alarm_and_cooldown(tmp_path, mode):
    watcher = Watcher(tmp_path)
    watcher.timeout = mode == "timeout"
    watcher.exit = 1
    assert watcher.tick().outcome == "failed"
    assert watcher.tick().outcome == "cooldown"
    assert len(watcher.recoveries()) == 1
    assert json.loads(watcher.snapshot.read_text())["last_tick"] == watcher.last


def test_unreadable_recovery_state_refuses_a_repeat_attempt(tmp_path):
    watcher = Watcher(tmp_path)
    watcher.record.write_text("broken")
    assert watcher.tick().outcome == "refused"
    assert not watcher.recoveries()


def test_a_new_attempt_is_allowed_only_after_the_cooldown(tmp_path):
    watcher = Watcher(tmp_path)
    watcher.tick()
    assert watcher.tick(NOW + timedelta(seconds=599)).outcome == "cooldown"
    assert watcher.tick(NOW + timedelta(seconds=600)).outcome == "attempted"
    assert len(watcher.recoveries()) == 2


def test_diagnostics_are_bounded_and_missing_logs_are_recorded(tmp_path):
    watcher = Watcher(tmp_path)
    watcher.log.write_text("tick old\n" * 20000 + "PR-INVENTORY STALE latest\n")
    result = watcher.tick(apply=False)
    assert len(result.log_evidence) == 60
    assert result.log_evidence[-1] == "PR-INVENTORY STALE latest"
    watcher.log.unlink()
    assert "unreadable" in watcher.tick(apply=False).log_evidence[0]


def test_failed_status_read_does_not_hide_the_inventory_failure(tmp_path):
    watcher = Watcher(tmp_path)

    def failing(argv, **kwargs):
        raise FileNotFoundError("status command")

    result = HandlerWatcherRecovery(watcher.options, run=failing).handle(
        ModelWatcherRecoveryRequest(now=NOW)
    )
    assert result.outcome == "failed"
    assert "FileNotFoundError" in result.inventory_status
    assert result.after_at == at(watcher.last)


def test_effect_preserves_alarm_after_successful_command_without_refresh(
    tmp_path, monkeypatch
):
    from omnimarket.nodes.node_silent_failure_alarm_effect.handlers import (
        handler_silent_failure_alarm as effect,
    )
    from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
        ModelSilentFailureAlarmOptions,
        ModelSilentFailureAlarmRequest,
    )

    watcher = Watcher(tmp_path)
    ledger = tmp_path / "ledger.md"
    ledger.write_text("")
    trend = tmp_path / "trend.json"
    trend.write_text(json.dumps({"last": {"ts": NOW.strftime("%Y-%m-%dT%H:%M:%SZ")}}))
    controller = tmp_path / "landing"
    controller.mkdir()
    receipts = controller / "ticks.jsonl"
    receipts.write_text(
        json.dumps(
            {"ts": NOW.isoformat(), "mode": "observe", "status": "OK", "detail": ""}
        )
        + "\n"
    )
    os.utime(receipts, (NOW.timestamp(), NOW.timestamp()))
    options = ModelSilentFailureAlarmOptions(
        registry_root=tmp_path,
        ledger=ledger,
        trend_file=trend,
        watcher_state=watcher.snapshot,
        watcher_recovery=watcher.options,
        controller_state_dir=controller,
        delivery_state_file=tmp_path / "deliveries.json",
        job_state_file=tmp_path / "jobs.json",
        hook_events_command=(sys.executable, "-c", ""),
        min_merges_per_hour=0,
        min_lanes_placed_per_hour=0,
    )
    handler = effect.HandlerSilentFailureAlarm(
        options,
        watcher_recovery=HandlerWatcherRecovery(watcher.options, run=watcher.run),
    )
    rows = []
    monkeypatch.setattr(
        effect,
        "append_row",
        lambda *args, **_kwargs: (rows.append(args[3]) is None, "accepted"),
    )
    monkeypatch.setattr(handler, "_deliver", lambda *_args: (True, "delivered", None))
    monkeypatch.setattr(handler, "_read_jobs", lambda: ((), True, False, ""))
    result = handler.handle(ModelSilentFailureAlarmRequest(now=NOW))
    assert "tick_liveness=STALE" in result.summary
    assert "watcher_recovery=attempted" in result.summary
    assert "tick-liveness" in result.alarms
    assert any("class=tick-liveness" in row for row in rows)
    assert json.loads(options.delivery_state_file.read_text())["open"]["tick-liveness"]
    # A real refresh ends the episode on a later tick; command exit zero alone did not.
    watcher.snapshot.write_text(
        json.dumps(
            {"last_tick": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "prs": {}, "merges": {}}
        )
    )
    after = handler.handle(
        ModelSilentFailureAlarmRequest(now=NOW + timedelta(seconds=1))
    )
    assert "tick_liveness=LIVE" in after.summary
    assert "resolved=tick-liveness" in after.summary
    assert any("resolved=tick-liveness" in row for row in rows)


def test_installer_preserves_existing_alarm_commands_and_branch_path(
    tmp_path, monkeypatch
):
    import plistlib

    from omnimarket.nodes.node_host_installer_registry_units.handlers import (
        handler_registry_units as installer,
    )
    from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
        HandlerRegistryUnits,
    )
    from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
        ModelRegistryUnitsRequest,
    )

    home = tmp_path / "user"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    existing = agents / "ai.omninode.silent-failure-alarm.plist"
    existing.write_bytes(
        plistlib.dumps(
            {
                "EnvironmentVariables": {
                    "ONEX_HOOK_EVENTS_COMMAND": "host-events-reader",
                    "ONEX_ALARM_DISPATCH_COMMAND": "repair-dispatch",
                    "ONEX_ALARM_HOST_ALIASES": '{"host-a":"node-a"}',
                    "ONEX_WORK_LEDGER_BUS_LANE": "sample-lane",
                    "UNDECLARED": "never-copy",
                }
            }
        )
    )
    handler = HandlerRegistryUnits()
    unit = handler.units["silent-failure-alarm"]
    request = ModelRegistryUnitsRequest(
        unit="silent-failure-alarm",
        home=str(home),
        registry_root=str(tmp_path / "registry"),
        project_path=str(tmp_path / "branch"),
        workspace_config_root=str(tmp_path / "config"),
    )
    env = handler.environment(request, unit)
    assert env["ONEX_HOOK_EVENTS_COMMAND"] == "host-events-reader"
    assert env["ONEX_ALARM_DISPATCH_COMMAND"] == "repair-dispatch"
    assert "UNDECLARED" not in env
    monkeypatch.setattr(installer, "interpreter_present", lambda _: True)
    text = handler.render(
        request, unit, tmp_path / "branch", "/usr/bin/uv", "/usr/bin/python3"
    )
    rendered = plistlib.loads(text.encode())
    assert str(tmp_path / "branch") in rendered["ProgramArguments"]
    assert (
        "omnimarket.nodes.node_silent_failure_alarm_effect"
        in rendered["ProgramArguments"]
    )
    assert rendered["Label"] == "ai.omninode.silent-failure-alarm"
    assert rendered["StartInterval"] == 600


def test_installer_refuses_to_drop_other_alarm_commands(tmp_path):
    from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
        HandlerRegistryUnits,
    )
    from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
        ModelRegistryUnitsRequest,
    )

    handler = HandlerRegistryUnits()
    request = ModelRegistryUnitsRequest(
        unit="silent-failure-alarm",
        home=str(tmp_path),
        registry_root=str(tmp_path),
        workspace_config_root=str(tmp_path / "config"),
    )
    with pytest.raises(ValueError, match="ONEX_WORK_LEDGER_BUS_LANE"):
        handler.environment(request, handler.units[request.unit])


def test_installed_entry_dispatches_the_real_effect_over_contract_topics(
    tmp_path, monkeypatch, capsys
):
    from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_silent_failure_alarm_cli import (
        main,
    )

    root = tmp_path / "registry"
    root.mkdir()
    ledger = root / "ledger.md"
    ledger.write_text("")
    trend = root / "trend.json"
    trend.write_text(json.dumps({"last": {"ts": NOW.strftime("%Y-%m-%dT%H:%M:%SZ")}}))
    watcher = root / "watcher.json"
    watcher.write_text(
        json.dumps(
            {"last_tick": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "prs": {}, "merges": {}}
        )
    )
    controller = root / "landing"
    controller.mkdir()
    receipt = controller / "ticks.jsonl"
    receipt.write_text(
        json.dumps(
            {"ts": NOW.isoformat(), "mode": "observe", "status": "OK", "detail": ""}
        )
        + "\n"
    )
    os.utime(receipt, (NOW.timestamp(), NOW.timestamp()))
    launchctl = root / "launchctl"
    launchctl.write_text("#!/bin/sh\nprintf 'PID Status Label\\n'\n")
    launchctl.chmod(0o755)
    state = root / "runtime"
    exit_code = main(
        [
            "--omni-home",
            str(root),
            "--ledger",
            str(ledger),
            "--watcher-state",
            str(watcher),
            "--trend-file",
            str(trend),
            "--controller-state-dir",
            str(controller),
            "--launchctl",
            str(launchctl),
            "--state-dir",
            str(state),
            "--hook-events-command",
            f"{sys.executable} -c ''",
            "--no-drainer",
            "--min-merges-per-hour",
            "0",
            "--min-lanes-placed-per-hour",
            "0",
            "--dry-run",
        ],
        now=lambda: NOW,
    )
    output = capsys.readouterr()
    assert exit_code == 0, output.err
    assert "tick_liveness=LIVE" in output.out
    assert (
        json.loads((state / "last-run.json").read_text())["watcher_recovery"]["outcome"]
        == "fresh"
    )
    workflow = json.loads((state / "workflow_result.json").read_text())
    assert workflow["handler_result"]["summary"].startswith("silent_failure_alarm:")


def test_default_recovery_uses_the_deployed_watcher_binding(tmp_path):
    import plistlib

    watcher = Watcher(tmp_path)
    service = tmp_path / "watcher.plist"
    service.write_bytes(
        plistlib.dumps(
            {
                "ProgramArguments": [
                    "/usr/bin/python3",
                    "-m",
                    "sample.watcher",
                    "serve",
                    "--bus-lane",
                    "service-lane",
                    "--omnibase-path",
                    "/srv/runtime",
                ],
                "EnvironmentVariables": {
                    "PYTHONPATH": "/srv/installed-library",
                    "ONEX_WORKSPACE_CONFIG_ROOT": "/srv/config",
                    "OMNI_HOME": "/srv/registry",
                    "TOKEN_VALUE": "never-propagate",
                },
            }
        )
    )
    options = watcher.options.model_copy(
        update={"status_command": (), "recovery_command": (), "watcher_plist": service}
    )
    result = HandlerWatcherRecovery(options, run=watcher.run).handle(
        ModelWatcherRecoveryRequest(now=NOW)
    )
    assert result.outcome == "attempted"
    command = watcher.recoveries()[0]
    assert command[:3] == ["env", "-u", "PYTHONPATH"]
    assert "PYTHONPATH=/srv/installed-library" in command
    assert "sample.watcher" in command
    assert command[command.index("--bus-lane") + 1] == "service-lane"
    assert command[command.index("--omnibase-path") + 1] == "/srv/runtime"
    assert "serve" not in command
    assert not any("TOKEN_VALUE" in part for part in command)


def test_unreadable_installed_watcher_refuses_to_guess_a_recovery_command(tmp_path):
    watcher = Watcher(tmp_path)
    options = watcher.options.model_copy(
        update={
            "status_command": (),
            "recovery_command": (),
            "watcher_plist": tmp_path / "missing.plist",
        }
    )
    result = HandlerWatcherRecovery(options, run=watcher.run).handle(
        ModelWatcherRecoveryRequest(now=NOW)
    )
    assert result.outcome == "refused"
    assert not watcher.calls
    assert result.after_at == at(watcher.last)


@pytest.mark.parametrize(
    ("status", "accepted"),
    [("accepted", True), ("duplicate", True), ("queued", False), ("refused", False)],
)
def test_ledger_append_uses_declared_lane_and_never_accepts_a_queue_as_receipt(
    tmp_path, monkeypatch, status, accepted
):
    from omnimarket.nodes.node_silent_failure_alarm_effect.handlers import (
        handler_alarm_host_io as io,
    )

    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"status": status}), "")

    monkeypatch.setattr(io.subprocess, "run", run)
    assert (
        io.append_row(
            sys.executable,
            None,
            tmp_path / "ledger",
            "row",
            registry_root=tmp_path,
            bus_lane="writer-lane",
        )[0]
        is accepted
    )
    assert calls[0][calls[0].index("--bus-lane") + 1] == "writer-lane"
    calls.clear()
    assert not io.append_row(
        sys.executable, None, tmp_path / "ledger", "row", registry_root=tmp_path
    )[0]
    assert not calls
