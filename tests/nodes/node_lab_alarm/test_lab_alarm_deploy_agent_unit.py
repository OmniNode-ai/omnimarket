# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deploy agent's systemd USER unit the dev-tag-heartbeat condition also reads (OMN-26316).

Golden: with the unit declared, an active unit plus a fresh beat reads OK and the reading names the
unit; a loaded unit whose state is not active breaches under the subject ``unit:<name>`` on the same
persist_runs bound and in the same streak store as a repository, so runs 1 and 2 wait and run 3
alarms with the unit's own alarm line, and a good read in between resets the streak. Error: a unit
that is not found in the user scope and a state that cannot be read are INDETERMINATE, never ALARM
and never OK (``is-active`` prints "inactive" for a unit that does not exist too, which is why only
a loaded unit's state can alarm); a stopped unit also makes the heartbeat unreadable, and the
unreadable heartbeat joins the unit breach's waiting reason until the streak reaches its bound.
Failing control: the same inputs with LoadState loaded and ActiveState inactive alarm at the bound,
so it is the not-found LoadState that holds the alarm back, not the ActiveState. Seam: the unit's
alarm line splits at the first ': ' into a valid ModelLabAlarm; SshUserUnitStateSource runs
``systemctl --user show`` through the overlay's host_command prefix, never a system-scope call, and
refuses a timeout, an OSError, a non-zero exit and an answer lacking a key; the overlay loads with
and without the unit block, and a run whose overlay declares no unit reads no unit state at all.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.models.lab_alarm_delivery import ModelLabAlarm
from omnimarket.nodes.node_lab_alarm.adapters import runtime_adapters
from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryGrowthStreaks,
    InMemoryHeartbeatSource,
    InMemoryUnitStateSource,
)
from omnimarket.nodes.node_lab_alarm.adapters.runtime_adapters import (
    SshUserUnitStateSource,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_dev_tag_heartbeat import (
    CONDITION,
    HandlerDevTagHeartbeat,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_dev_tag_heartbeat import (
    EnumDevTagHeartbeatFault,
    EnumDevTagHeartbeatOutcome,
    ModelDevTagHeartbeatCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    ModelConditionPersistence,
    ModelDeployAgentUnitSettings,
    ModelDevTagHeartbeatSettings,
    ModelLabAlarmOverlay,
)
from omnimarket.nodes.node_lab_alarm.protocols import UnitStateUnreadableError

pytestmark = pytest.mark.unit

_NODE = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_lab_alarm"
_CLOCK = datetime(2026, 11, 23, 5, 0, tzinfo=UTC)
_REPO = "example-repo"
_UNIT = "deploy-agent-dev.service"
_KEY = f"unit:{_UNIT}"
_PREFIX = ("ssh", "-o", "BatchMode=yes", "example-host")


def _unit() -> ModelDeployAgentUnitSettings:
    return ModelDeployAgentUnitSettings(
        name=_UNIT,
        host_command=_PREFIX,
        timeout_seconds=10,
    )


def _settings(
    persist_runs: int = 1,
    *,
    unit: ModelDeployAgentUnitSettings | None = None,
) -> ModelDevTagHeartbeatSettings:
    return ModelDevTagHeartbeatSettings(
        agent_url="http://agent.example:8080",
        endpoint_path="/v1/heartbeat",
        repositories=(_REPO,),
        max_age_seconds=300,
        timeout_seconds=10,
        persistence=ModelConditionPersistence(
            persist_runs=persist_runs,
            streak_path=".onex_state/lab-alarm/dev-tag-heartbeat-streaks.json",
        ),
        unit=unit,
    )


def _command(
    *,
    write: bool = True,
    fault: EnumDevTagHeartbeatFault = EnumDevTagHeartbeatFault.NONE,
) -> ModelDevTagHeartbeatCommand:
    return ModelDevTagHeartbeatCommand(
        correlation_id="unit-1", write=write, fault=fault
    )


def _fresh_document() -> dict[str, object]:
    return {
        "heartbeat": {
            _REPO: {
                "observed_at": _CLOCK.isoformat(),
                "alarm": False,
                "newest_tag": "dev-42",
                "running_ref": "dev-41",
                "lag_seconds": 0,
                "alarm_after_seconds": 3600,
            }
        }
    }


def _alarmed_document() -> dict[str, object]:
    return {
        "heartbeat": {
            _REPO: {
                "observed_at": _CLOCK.isoformat(),
                "alarm": True,
                "newest_tag": "dev-42",
                "running_ref": "dev-41",
                "lag_seconds": 900,
                "alarm_after_seconds": 600,
            }
        }
    }


class _Rig:
    """One handler over the in-memory fixtures: a heartbeat document, a unit state, a streak store."""

    def __init__(
        self,
        *,
        persist_runs: int = 1,
        declares_unit: bool = True,
        load_state: str = "loaded",
        active_state: str = "active",
        unit_error: str | None = None,
    ) -> None:
        self.unit = InMemoryUnitStateSource(
            load_state=load_state, active_state=active_state, error=unit_error
        )
        self.source = InMemoryHeartbeatSource(document=_fresh_document())
        self.streaks = InMemoryGrowthStreaks()
        self.handler = HandlerDevTagHeartbeat(
            settings=_settings(persist_runs, unit=_unit() if declares_unit else None),
            source=self.source,
            streaks=self.streaks,
            clock=lambda: _CLOCK,
            unit_source=self.unit,
        )


def test_active_unit_with_a_fresh_beat_reads_ok() -> None:
    """An active unit is a good reading that names the unit; the beat still decides the rest."""
    rig = _Rig()
    result = rig.handler.handle(_command())
    assert result.outcome is EnumDevTagHeartbeatOutcome.OK
    assert result.alarms == ()
    assert result.reasons == ()
    assert f"unit {_UNIT}: active" in result.readings
    assert f"{_REPO}: fresh" in result.readings
    assert rig.streaks.streaks == {}
    assert rig.unit.reads == 1


def test_inactive_unit_alarms_once_its_streak_reaches_the_bound() -> None:
    """A loaded unit that is not active breaches like a repository: runs 1 and 2 wait, run 3 alarms."""
    rig = _Rig(persist_runs=3, active_state="inactive")
    first = rig.handler.handle(_command())
    assert first.outcome is EnumDevTagHeartbeatOutcome.OK
    assert first.reasons == (f"waiting:{_KEY}: run 1 of 3",)
    assert f"unit {_UNIT}: inactive" in first.readings
    assert f"{_REPO}: fresh" in first.readings
    assert rig.streaks.streaks == {_KEY: 1}
    second = rig.handler.handle(_command())
    assert second.outcome is EnumDevTagHeartbeatOutcome.OK
    assert second.reasons == (f"waiting:{_KEY}: run 2 of 3",)
    third = rig.handler.handle(_command())
    assert third.outcome is EnumDevTagHeartbeatOutcome.ALARM
    assert third.alarms == (
        f"dev-tag-heartbeat:{_KEY}: the deploy agent's user unit {_UNIT} reads "
        "inactive (user scope), not active",
    )
    assert third.reasons == ()


def test_a_good_unit_read_in_between_resets_the_streak() -> None:
    """An active read drops the unit's streak, so the counting restarts at one after it."""
    rig = _Rig(persist_runs=3, active_state="inactive")
    rig.handler.handle(_command())
    rig.handler.handle(_command())
    assert rig.streaks.streaks == {_KEY: 2}
    rig.unit.active_state = "active"
    good = rig.handler.handle(_command())
    assert good.outcome is EnumDevTagHeartbeatOutcome.OK
    assert good.reasons == ()
    assert rig.streaks.streaks == {}, "the active read cleared the unit streak"
    rig.unit.active_state = "inactive"
    restart = rig.handler.handle(_command())
    assert restart.outcome is EnumDevTagHeartbeatOutcome.OK
    assert restart.reasons == (f"waiting:{_KEY}: run 1 of 3",)
    rig.handler.handle(_command())
    alarm = rig.handler.handle(_command())
    assert alarm.outcome is EnumDevTagHeartbeatOutcome.ALARM


def test_not_found_is_indeterminate_never_alarm() -> None:
    """A unit missing from the user scope is a wrong name or host, not a down agent: never an alarm."""
    rig = _Rig(load_state="not-found", active_state="inactive")
    for _ in range(3):
        result = rig.handler.handle(_command())
        assert result.outcome is EnumDevTagHeartbeatOutcome.INDETERMINATE
        assert result.reasons == (f"unit-not-found:{_UNIT} (user scope)",)
        assert result.alarms == ()
    assert rig.streaks.streaks == {}
    assert rig.source.reads == 0, "the not-found verdict reads no heartbeat"


def test_not_found_failing_control_a_loaded_inactive_unit_alarms() -> None:
    """The control of the not-found read: the same ActiveState on a loaded unit alarms at the bound,
    so it is the not-found LoadState that holds the alarm back, not the ActiveState."""
    rig = _Rig(load_state="loaded", active_state="inactive")
    result = rig.handler.handle(_command())
    assert result.outcome is EnumDevTagHeartbeatOutcome.ALARM


def test_unreadable_unit_state_is_indeterminate() -> None:
    """A state that cannot be read says nothing about the unit: INDETERMINATE, never an alarm."""
    rig = _Rig(unit_error="ssh: connect to host timed out")
    result = rig.handler.handle(_command())
    assert result.outcome is EnumDevTagHeartbeatOutcome.INDETERMINATE
    assert result.reasons == ("unit-unreadable",)
    assert result.alarms == ()


def test_inactive_unit_with_unreadable_heartbeat_alarms_at_the_bound() -> None:
    """A stopped unit stops the heartbeat too: the unit breach keeps its streak, the unreadable
    heartbeat joins the waiting reason, and the bound still decides when the run alarms."""
    rig = _Rig(persist_runs=3, active_state="inactive")
    rig.source.error = "connection refused"
    first = rig.handler.handle(_command())
    assert first.outcome is EnumDevTagHeartbeatOutcome.INDETERMINATE
    assert first.reasons == (
        "heartbeat-unreadable",
        f"waiting:{_KEY}: run 1 of 3",
    )
    assert first.alarms == ()
    assert rig.streaks.streaks == {_KEY: 1}
    second = rig.handler.handle(_command())
    assert second.outcome is EnumDevTagHeartbeatOutcome.INDETERMINATE
    assert second.reasons == (
        "heartbeat-unreadable",
        f"waiting:{_KEY}: run 2 of 3",
    )
    third = rig.handler.handle(_command())
    assert third.outcome is EnumDevTagHeartbeatOutcome.ALARM
    assert third.alarms == (
        f"dev-tag-heartbeat:{_KEY}: the deploy agent's user unit {_UNIT} reads "
        "inactive (user scope), not active",
    )


def test_no_declared_unit_runs_as_the_condition_always_ran() -> None:
    """An overlay without the unit block reads the heartbeat alone, exactly as before OMN-26316."""
    rig = _Rig(declares_unit=False)
    result = rig.handler.handle(_command())
    assert result.outcome is EnumDevTagHeartbeatOutcome.OK
    assert result.readings == (f"{_REPO}: fresh",)
    assert rig.unit.reads == 0, "no declared unit reads no unit state"
    rig.source.document = _alarmed_document()
    breach = rig.handler.handle(_command())
    assert breach.outcome is EnumDevTagHeartbeatOutcome.ALARM
    [alarm] = breach.alarms
    assert alarm.startswith(f"dev-tag-heartbeat:{_REPO}: ")
    assert not any(reading.startswith("unit ") for reading in breach.readings)


def test_a_declared_unit_without_a_source_reads_only_the_heartbeat() -> None:
    """The unit is read only when the wiring hands the handler a unit source too."""
    handler = HandlerDevTagHeartbeat(
        settings=_settings(unit=_unit()),
        source=InMemoryHeartbeatSource(document=_fresh_document()),
        streaks=InMemoryGrowthStreaks(),
        clock=lambda: _CLOCK,
    )
    assert handler.handle(_command()).outcome is EnumDevTagHeartbeatOutcome.OK


def test_a_drill_reads_no_unit_state() -> None:
    """The stale-beat drill reads nothing: not the unit, not the heartbeat, and it keeps no streak."""
    rig = _Rig()
    result = rig.handler.handle(_command(fault=EnumDevTagHeartbeatFault.STALE_BEAT))
    assert result.outcome is EnumDevTagHeartbeatOutcome.ALARM
    assert rig.unit.reads == 0
    assert rig.source.reads == 0
    assert rig.streaks.streaks == {}


def test_the_unit_alarm_line_builds_a_valid_lab_alarm() -> None:
    """The unit's alarm line splits at the first ': ' into the subject the delivery keys on."""
    rig = _Rig(active_state="failed")
    result = rig.handler.handle(_command())
    [alarm] = result.alarms
    subject, separator, detail = alarm.partition(": ")
    assert separator == ": "
    assert subject == f"dev-tag-heartbeat:{_KEY}"
    assert detail == (
        f"the deploy agent's user unit {_UNIT} reads failed (user scope), not active"
    )
    model = ModelLabAlarm(condition=CONDITION, subject=subject, detail=detail)
    assert model.condition == CONDITION
    assert model.subject == subject
    assert model.detail == detail


def test_ssh_user_unit_source_runs_systemctl_user_show_through_the_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe reads USER scope with the exact unit name: systemctl --user show behind the prefix."""
    captured: list[list[str]] = []

    def _run(
        argv: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured.append(list(argv))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="LoadState=loaded\nActiveState=active\n",
            stderr="",
        )

    monkeypatch.setattr(runtime_adapters.subprocess, "run", _run)
    source = SshUserUnitStateSource(_PREFIX, _UNIT, 10.0)
    assert source.read() == ("loaded", "active")
    [argv] = captured
    assert argv[: len(_PREFIX)] == list(_PREFIX)
    show = argv.index("systemctl")
    assert argv[show + 1] == "--user"
    assert argv[show + 2] == "show"
    assert _UNIT in argv
    assert "-p" in argv
    assert "LoadState" in argv
    assert "ActiveState" in argv
    assert "--system" not in argv, (
        "the scope is fixed to the user one, never the system one"
    )


def test_ssh_user_unit_source_refuses_what_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout, an OSError, a non-zero exit and an answer lacking a key are unreadable, typed."""

    def _timeout(
        argv: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 3)

    monkeypatch.setattr(runtime_adapters.subprocess, "run", _timeout)
    with pytest.raises(UnitStateUnreadableError, match="TimeoutExpired"):
        SshUserUnitStateSource(_PREFIX, _UNIT, 3.0).read()

    def _oserror(
        argv: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise OSError("no such file or directory")

    monkeypatch.setattr(runtime_adapters.subprocess, "run", _oserror)
    with pytest.raises(UnitStateUnreadableError, match="OSError"):
        SshUserUnitStateSource(_PREFIX, _UNIT, 3.0).read()

    def _exited(
        argv: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="Failed to connect to bus"
        )

    monkeypatch.setattr(runtime_adapters.subprocess, "run", _exited)
    with pytest.raises(UnitStateUnreadableError, match="exited 1"):
        SshUserUnitStateSource(_PREFIX, _UNIT, 3.0).read()

    def _partial(
        argv: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 0, stdout="LoadState=loaded\n", stderr=""
        )

    monkeypatch.setattr(runtime_adapters.subprocess, "run", _partial)
    with pytest.raises(UnitStateUnreadableError, match="ActiveState"):
        SshUserUnitStateSource(_PREFIX, _UNIT, 3.0).read()


def _example_raw() -> dict[str, object]:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_the_overlay_loads_with_and_without_the_unit_block() -> None:
    """The unit block is optional: an overlay written before OMN-26316 still loads, unit None."""
    overlay = ModelLabAlarmOverlay.model_validate(_example_raw())
    assert overlay.dev_tag_heartbeat is not None
    assert overlay.dev_tag_heartbeat.unit == _unit()
    raw = _example_raw()
    block = raw["dev_tag_heartbeat"]
    assert isinstance(block, dict)
    del block["unit"]
    without = ModelLabAlarmOverlay.model_validate(raw)
    assert without.dev_tag_heartbeat is not None
    assert without.dev_tag_heartbeat.unit is None
