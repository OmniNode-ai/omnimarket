# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the landing snapshot step is a declared launchd unit on the controller's host (OMN-23458). Until this unit existed the step ran only by hand; at the merge base the installer knows no landing-snapshot unit and the render test fails."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.runtime_snapshot import main
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    BREW_PYTHON,
    Recorder,
    make_checkout,
    plist_of,
    request,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def canonical(tmp_path: Path) -> Path:
    path = tmp_path / "omni" / "omnimarket"
    make_checkout(path)
    return path


UNIT = "landing-snapshot"
ENTRY = "src/omnimarket/nodes/node_pr_landing_snapshot_effect/__main__.py"


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def test_the_unit_is_declared_every_minute_on_launchd_and_runs_the_step_module() -> (
    None
):
    unit = load_units()[UNIT]
    assert unit["platform"] == "launchd"
    assert unit["schedule"]["start_interval"] == 60
    assert unit["module"] == "omnimarket.nodes.node_pr_landing_snapshot_effect"
    assert unit["entry_file"] == ENTRY
    assert unit["run_timeout_s"] < 60
    assert unit["environment"]["ONEX_SKILL_OVERLAY_ROOTS"].startswith(
        "{registry_root}/overlays/overlays"
    )
    assert unit["environment"]["OMNIBASE_PATH"] == "{registry_root}"


@pytest.mark.usefixtures("canonical")
def test_install_renders_the_unit_running_the_step_from_the_branch_checkout(
    tmp_path: Path,
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=ENTRY)
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    plist = plist_of(result.rendered)
    assert plist["Label"] == "ai.omninode.landing-snapshot"
    assert plist["StartInterval"] == 60
    assert plist["RunAtLoad"] is True
    args = plist["ProgramArguments"]
    assert (
        args[args.index("-m") + 1] == "omnimarket.nodes.node_pr_landing_snapshot_effect"
    )
    assert str(branch) in args
    env = plist["EnvironmentVariables"]
    assert env["OMNIBASE_PATH"] == str(tmp_path / "omni")
    assert env["ONEX_SKILL_OVERLAY_ROOTS"].startswith(
        str(tmp_path / "omni" / "overlays" / "overlays")
    )


@pytest.mark.usefixtures("canonical")
def test_a_checkout_without_the_step_entry_file_is_refused(tmp_path: Path) -> None:
    branch = tmp_path / "branch"
    make_checkout(branch, entry="src/omnimarket/nodes/other/__main__.py")
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(branch),
        )
    )
    assert result.status == "failure"


@pytest.mark.usefixtures("canonical")
def test_the_step_main_accepts_the_arguments_the_installer_proof_and_the_plist_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Seam: installer (start proof and plist) to the step's main. Either side changing its arguments fails here."""
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=ENTRY)
    recorder = Recorder()
    result = HandlerRegistryUnits(recorder).handle(
        request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    module = "omnimarket.nodes.node_pr_landing_snapshot_effect"
    proofs = [c for c in recorder.calls if c[-3:-1] == ["-m", module]]
    assert len(proofs) == 1
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    with pytest.raises(SystemExit) as raised:
        main(proofs[0][-1:])
    assert raised.value.code == 0
    assert "overlay_refused" not in capsys.readouterr().out
    plist_args = plist_of(result.rendered)["ProgramArguments"]
    unit_args = plist_args[plist_args.index(module) + 1 :]
    assert unit_args[0] == "--omni-home"
    missing_facts = tmp_path / "overlay-roots" / "node_pr_landing_snapshot_effect"
    missing_facts.mkdir(parents=True)
    (missing_facts / "overlay.yaml").write_text(
        "\n".join(
            [
                "backend: redpanda",
                "record_topic: t.record",
                "failed_topic: t.failed",
                "floor_piece: 9",
                "bus_lane: dev",
                f"workspace_root: {tmp_path}",
                f"facts_path: {tmp_path}/absent-facts.json",
                f"watcher_state_path: {tmp_path}/state.json",
                "watcher_max_age_seconds: 900",
                "owner: acme",
                f"record_journal: {tmp_path}/records.jsonl",
                f"emission_journal: {tmp_path}/emissions.jsonl",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlay-roots"))
    code = main(unit_args)
    assert code == 3
    assert json.loads(capsys.readouterr().out)["status"] == "facts_unread"
