# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: ``--dry-run`` holds against every host-changing verb (OMN-19998).

The failing control is ``<unit> --dry-run --uninstall``: the mode flags were last-flag-wins, so the uninstall ran
(h201, 2026-11-13: a dry run removed a live unit). Now a ``--dry-run`` anywhere on the command line with install,
kickstart, uninstall or rollback gives a dry run: the plan is printed and no file is written, no unit is loaded or
unloaded and no launchctl or systemctl call is made. Without ``--dry-run`` the last flag still wins.
"""

from __future__ import annotations

import subprocess
import typing
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_host_installer_registry_units import (
    runtime_registry_units_cli as cli,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers import (
    handler_registry_units,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / "node_host_installer_registry_units"
HANDLER = "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units"
VERBS = ("install", "kickstart", "uninstall", "rollback")
# One launchd tick unit, one systemd unit and one copied-tree unit. The install dry run of the other two reads inputs
# this test does not stage (a host interpreter, the overlay host table); the held verbs return their plan before reading any.
CASES = [
    (unit, verb)
    for unit in (
        "factory-floor-publish",
        "overlay-store-refresh-systemd",
        "floor-alarm",
    )
    for verb in VERBS
    if unit == "factory-floor-publish" or verb != "install"
]
SERVICE_MANAGERS = ("launchctl", "systemctl")


def contract() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    return document


@pytest.fixture(autouse=True)
def interpreters_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{HANDLER}.interpreter_present", lambda _path: True)


class Recorder:
    """The injected runner: it records every command and answers success, so nothing reaches the host."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def service_manager_calls(self) -> list[list[str]]:
        return [
            call
            for call in self.calls
            if any(part in SERVICE_MANAGERS for part in call[:2])
        ]


def files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def request_for(tmp_path: Path, unit: str, *flags: str) -> ModelRegistryUnitsRequest:
    (tmp_path / "reg").mkdir(exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    return cli.build_request(
        [unit, *flags],
        {
            "OMNI_HOME": str(tmp_path / "reg"),
            "ONEX_WORKSPACE_CONFIG_ROOT": str(tmp_path / "ws"),
            "ONEX_PLANS_KB_PATH": str(tmp_path / "plans"),
            "ONEX_MARKETPLACE_ROOT": str(tmp_path / "market"),
            "ONEX_PLUGIN_ROOT": str(tmp_path / "plugin"),
        },
        str(tmp_path / "home"),
    )


@pytest.mark.parametrize(("unit", "verb"), CASES)
@pytest.mark.parametrize(
    "dry_run_first", [True, False], ids=["dry-run-first", "verb-first"]
)
def test_golden_a_dry_run_with_any_verb_makes_no_file_and_no_service_manager_call(
    tmp_path: Path, unit: str, verb: str, dry_run_first: bool
) -> None:
    flags = ("--dry-run", f"--{verb}") if dry_run_first else (f"--{verb}", "--dry-run")
    request = request_for(tmp_path, unit, *flags)
    recorder = Recorder()
    before = files(tmp_path)
    result = HandlerRegistryUnits(run=recorder).handle(request)
    assert request.mode == "dry-run"
    assert result.mode == "dry-run"
    assert result.exit_code == 0, result.stderr
    assert recorder.service_manager_calls() == []
    assert files(tmp_path) == before


@pytest.mark.parametrize("verb", ["kickstart", "uninstall", "rollback"])
def test_golden_the_plan_names_the_unit_the_label_and_the_verb(
    tmp_path: Path, verb: str
) -> None:
    request = request_for(tmp_path, "factory-floor-publish", "--dry-run", f"--{verb}")
    result = HandlerRegistryUnits(run=Recorder()).handle(request)
    assert request.plan_verb == verb
    assert f"dry-run: would {verb} factory-floor-publish" in result.stdout
    assert "ai.omninode.factory-floor-publish" in result.stdout
    assert "nothing was written, loaded, unloaded or run" in result.stdout


def test_control_the_old_parse_ran_the_last_flag_and_that_flag_order_now_holds(
    tmp_path: Path,
) -> None:
    """``--dry-run --uninstall`` was an uninstall; ``--install --uninstall`` (no dry run) still is one."""
    held = request_for(tmp_path, "factory-floor-publish", "--dry-run", "--uninstall")
    assert (held.mode, held.plan_verb) == ("dry-run", "uninstall")
    recorder = Recorder()
    live = request_for(tmp_path, "factory-floor-publish", "--install", "--uninstall")
    assert (live.mode, live.plan_verb) == ("uninstall", None)
    HandlerRegistryUnits(run=recorder).handle(live)
    assert any(call[:2] == ["launchctl", "bootout"] for call in recorder.calls)


def test_a_dry_run_beside_a_read_only_mode_keeps_the_last_flag() -> None:
    assert cli.resolve_mode(["dry-run", "status"], ["install", "uninstall"]) == (
        "status",
        None,
    )
    assert cli.resolve_mode(["status", "dry-run"], ["install", "uninstall"]) == (
        "dry-run",
        None,
    )
    assert cli.resolve_mode(None, ["install"]) == (None, None)


def test_error_a_plan_verb_without_a_dry_run_mode_is_refused() -> None:
    with pytest.raises(ValidationError, match="plan_verb"):
        ModelRegistryUnitsRequest(
            unit="u",
            mode="uninstall",
            home="/h",
            registry_root="/r",
            plan_verb="uninstall",
        )


def test_error_a_dry_run_of_an_unknown_unit_is_still_refused(tmp_path: Path) -> None:
    request = request_for(tmp_path, "no-such-unit", "--dry-run", "--uninstall")
    result = HandlerRegistryUnits(run=Recorder()).handle(request)
    assert result.exit_code == 2
    assert "not a unit of this node" in result.stderr


def test_seam_the_verbs_a_dry_run_holds_are_the_modes_that_change_the_host() -> None:
    held = set(contract()["command_line"]["held_by_dry_run"])
    mode_values = set(
        typing.get_args(ModelRegistryUnitsRequest.model_fields["mode"].annotation)
    )
    assert held == set(handler_registry_units.LOCKED_MODES) | {"kickstart"}
    assert held <= mode_values
    assert {"dry-run", "status"}.isdisjoint(held)
    plan_values = set(
        typing.get_args(
            typing.get_args(
                ModelRegistryUnitsRequest.model_fields["plan_verb"].annotation
            )[0]
        )
    )
    assert plan_values == held - {"install"}


def test_golden_the_command_runs_the_held_dry_run_through_the_node_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main`` builds the request, dispatches it over the in-memory bus to the real handler and prints the plan."""
    (tmp_path / "reg").mkdir()
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "reg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    before = files(tmp_path)
    code = cli.main(["factory-floor-publish", "--dry-run", "--uninstall"])
    out = capsys.readouterr().out
    assert code == 0
    assert "dry-run: would uninstall factory-floor-publish" in out
    assert files(tmp_path) == before
