# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One schedule per condition (OMN-24400): each condition's unit runs its own verb on its own cadence."""

from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_lab_alarm import runtime_lab_alarm
from omnimarket.nodes.node_lab_alarm.handlers.schedule import (
    BROKER_LAG_VERB,
    LabAlarmScheduler,
    render_launchd,
    render_systemd,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    SCHEDULABLE_VERBS,
    ModelLabAlarmOverlay,
)

_NODE = Path(runtime_lab_alarm.__file__).parent
_MAC = "example-mac"
_LINUX = "example-linux"


def _raw() -> dict[str, Any]:
    loaded = yaml.safe_load(
        (_NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def _overlay() -> ModelLabAlarmOverlay:
    return ModelLabAlarmOverlay.model_validate(_raw())


class _Runner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "", "")


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "registry"
    python = root / "examplepkg/.venv/bin/python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    return root


def _block(verb: str) -> Any:
    block = (_overlay().condition_schedules or {}).get(verb)
    assert block is not None, f"the example overlay declares no schedule for {verb}"
    return block


@pytest.mark.parametrize("verb", SCHEDULABLE_VERBS)
def test_condition_schedule_golden_launchd_agent_runs_the_condition_verb(
    tmp_path: Path, verb: str
) -> None:
    overlay = _overlay()
    block = _block(verb)
    scoped = overlay.model_copy(update={"schedule": block})
    host = block.host(_MAC)
    assert host is not None
    body = plistlib.loads(
        render_launchd(
            scoped,
            host,
            omni_root=_root(tmp_path),
            home=tmp_path / "home",
            overlay_root=tmp_path / "overlays",
            verb=verb,
        )
    )
    assert body["ProgramArguments"][-1] == verb
    assert body["Label"] == block.launchd_label
    assert body["StartInterval"] == block.interval_seconds
    assert body["StandardOutPath"].endswith(f"lab-alarm-{verb}.out.log")


@pytest.mark.parametrize("verb", SCHEDULABLE_VERBS)
def test_condition_schedule_golden_systemd_unit_runs_the_condition_verb(
    tmp_path: Path, verb: str
) -> None:
    block = _block(verb)
    scoped = _overlay().model_copy(update={"schedule": block})
    host = block.host(_LINUX)
    assert host is not None
    service, timer = render_systemd(
        scoped,
        host,
        omni_root=_root(tmp_path),
        home=tmp_path / "home",
        overlay_root=tmp_path / "overlays",
        verb=verb,
    )
    assert f" -m omnimarket.nodes.node_lab_alarm {verb}\n" in service
    assert f"OnUnitActiveSec={block.interval_seconds}s" in timer
    assert f"Unit={block.unit_name}.service" in timer


def test_condition_schedule_the_broker_lag_unit_is_unchanged(tmp_path: Path) -> None:
    overlay = _overlay()
    host = overlay.schedule.host(_MAC)
    assert host is not None
    body = plistlib.loads(
        render_launchd(
            overlay,
            host,
            omni_root=_root(tmp_path),
            home=tmp_path / "home",
            overlay_root=tmp_path / "overlays",
        )
    )
    assert body["ProgramArguments"][-1] == BROKER_LAG_VERB
    assert body["StandardOutPath"].endswith("/lab-alarm.out.log")


def test_condition_schedule_cli_install_dry_run_renders_the_conditions_own_unit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    overlay = _overlay()
    code = runtime_lab_alarm.run_schedule(
        "install",
        _MAC,
        overlay=overlay,
        condition="dev-tag-heartbeat",
        omni_root=_root(tmp_path),
        overlay_root=tmp_path / "overlays",
        dry_run=True,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "example.lab-alarm-dev-tag-heartbeat" in out
    assert "example.lab-alarm\n" not in out


def test_condition_schedule_real_install_loads_the_conditions_own_label(
    tmp_path: Path,
) -> None:
    runner = _Runner()
    block = _block("delegation-chain-canary")
    scheduler = LabAlarmScheduler(
        overlay=_overlay().model_copy(update={"schedule": block}),
        home=tmp_path / "home",
        run=runner,
        uid=501,
        verb="delegation-chain-canary",
    )
    result = scheduler.install(
        _MAC, omni_root=_root(tmp_path), overlay_root=tmp_path / "overlays"
    )
    assert result.exit_code == 0, result.stderr
    assert ["launchctl", "bootout", f"gui/501/{block.launchd_label}"] in runner.commands
    assert (
        tmp_path / "home/Library/LaunchAgents" / f"{block.launchd_label}.plist"
    ).is_file()


def test_condition_schedule_error_a_condition_with_no_declared_schedule_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = _raw()
    del raw["condition_schedules"]["dev-tag-heartbeat"]
    code = runtime_lab_alarm.run_schedule(
        "install",
        _MAC,
        overlay=ModelLabAlarmOverlay.model_validate(raw),
        condition="dev-tag-heartbeat",
        omni_root=_root(tmp_path),
        overlay_root=tmp_path / "overlays",
        dry_run=True,
    )
    assert code == 1
    assert "declares no schedule for this condition" in capsys.readouterr().err


def test_condition_schedule_error_a_host_the_condition_does_not_declare_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = runtime_lab_alarm.run_schedule(
        "install",
        "not-a-host",
        overlay=_overlay(),
        condition="dev-tag-heartbeat",
        omni_root=_root(tmp_path),
        overlay_root=tmp_path / "overlays",
        dry_run=True,
    )
    assert code == 1
    assert (
        "not in the node_lab_alarm overlay's schedule hosts" in capsys.readouterr().err
    )


def test_condition_schedule_failing_control_two_schedules_may_not_share_a_unit() -> (
    None
):
    raw = _raw()
    schedules = raw["condition_schedules"]
    schedules["delegation-chain-canary"]["unit_name"] = schedules["dev-tag-heartbeat"][
        "unit_name"
    ]
    with pytest.raises(ValidationError, match="share a unit name"):
        ModelLabAlarmOverlay.model_validate(raw)


def test_condition_schedule_failing_control_a_schedule_for_an_unknown_verb_is_refused() -> (
    None
):
    raw = _raw()
    raw["condition_schedules"]["broker-lag"] = raw["condition_schedules"][
        "dev-tag-heartbeat"
    ]
    with pytest.raises(ValidationError, match="not a schedulable verb"):
        ModelLabAlarmOverlay.model_validate(raw)


@pytest.mark.parametrize("verb", SCHEDULABLE_VERBS)
def test_condition_schedule_seam_every_scheduled_verb_is_a_verb_and_an_operation(
    verb: str,
) -> None:
    """The unit runs ``python -m ... <verb>``: the verb must parse, and the contract must bind its operation."""
    with pytest.raises(SystemExit) as parsed:
        runtime_lab_alarm.main([verb, "--help"])
    assert parsed.value.code == 0
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    operations = {entry["operation"] for entry in contract["cli_handlers"]}
    assert verb.replace("-", "_") in operations
    assert verb in SCHEDULABLE_VERBS
