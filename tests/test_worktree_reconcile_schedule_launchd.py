# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A Mac host the overlay declares schedules the same node tick through launchd (OMN-22799)."""

import json
import plistlib
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.events.worktree_reconcile import (
    ModelWorktreeReconcileRunCompletedEvent,
    ModelWorktreeReconcileRunResult,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_launchd_units import (
    LaunchdUnitRootReader,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.handler_worktree_schedule import (
    HandlerWorktreeSchedule,
)
from omnimarket.nodes.node_worktree_reconcile_effect.models.model_worktree_schedule import (
    ModelWorktreeScheduleOverlay,
    ModelWorktreeScheduleRequest,
    ModelWorktreeScheduleResult,
)
from tests.test_worktree_reconcile_schedule import overlay

pytestmark = pytest.mark.unit

LABEL = "ai.omninode.worktree-reconcile"
NODE_DIR = Path(__file__).resolve().parents[1] / (
    "src/omnimarket/nodes/node_worktree_reconcile_effect"
)


def contract_schedule() -> dict:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text())["schedule"]


def launchd_overlay(tmp_path: Path) -> Path:
    path = overlay(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["schedulers"] = {"lab-host": "launchd"}
    path.write_text(yaml.safe_dump(document))
    env_file = tmp_path / "host.env"
    env_file.write_text("SECRET_NAME=secret-value-xyz\n")
    document["environment_file"] = str(env_file)
    path.write_text(yaml.safe_dump(document))
    return path


def worktree_overlay(tmp_path: Path) -> Path:
    """The same overlay with the project moved under the canonical worktree segment."""
    path = overlay(tmp_path)
    worktrees = tmp_path / "omni_worktrees"
    worktrees.mkdir()
    (tmp_path / "project").rename(worktrees / "project")
    document = yaml.safe_load(path.read_text())
    document["schedulers"] = {"lab-host": "launchd"}
    document["project"] = str(worktrees / "project")
    path.write_text(yaml.safe_dump(document))
    return path


def render(tmp_path: Path, path: Path) -> ModelWorktreeScheduleResult:
    return HandlerWorktreeSchedule(
        launchd_directory=tmp_path / "LaunchAgents", uid=4242
    ).handle(
        ModelWorktreeScheduleRequest(
            mode="render", host="lab-host", overlay_path=str(path)
        )
    )


def test_render_launchd_host_is_an_agent_plist(tmp_path: Path) -> None:
    path = launchd_overlay(tmp_path)
    result = render(tmp_path, path)
    assert result.exit_code == 0
    assert result.service == ""
    assert result.timer == ""
    assert result.dropin == ""
    document = plistlib.loads(result.plist.encode())
    schedule = contract_schedule()
    assert document["Label"] == schedule["launchd_label"]
    assert document["StartInterval"] == schedule["interval_seconds"]
    assert document["Nice"] == schedule["resources"]["nice"]
    arguments = document["ProgramArguments"]
    assert "tick" in arguments
    assert "lab-host" in arguments
    assert "--overlay" in arguments
    assert str(path) in arguments
    environment = document["EnvironmentVariables"]
    assert environment == {"PYTHONPATH": f"{tmp_path / 'project'}/src"}
    # The host environment file is sourced by the shell, never copied into the world-readable
    # plist: its values and names must not appear, only the file path the shell sources.
    overlay_document = yaml.safe_load(path.read_text())
    env_file = overlay_document["environment_file"]
    assert env_file in arguments
    assert "SECRET_NAME" not in environment
    assert "secret-value-xyz" not in result.plist
    assert "SECRET_NAME" not in result.plist
    # Failing control: the same overlay without schedulers still renders the systemd
    # unit and carries no plist, so the declared manager is what changed the render.
    overlay_document = yaml.safe_load(path.read_text())
    del overlay_document["schedulers"]
    path.write_text(yaml.safe_dump(overlay_document))
    systemd = render(tmp_path, path)
    assert systemd.exit_code == 0
    assert systemd.plist == ""
    assert systemd.service
    assert systemd.service != result.plist


def test_install_writes_the_plist_and_loads_it_in_order(tmp_path: Path) -> None:
    path = launchd_overlay(tmp_path)
    calls: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    agents = tmp_path / "LaunchAgents"
    result = HandlerWorktreeSchedule(
        launchd_directory=agents, uid=4242, runner=run
    ).handle(
        ModelWorktreeScheduleRequest(
            mode="install", host="lab-host", overlay_path=str(path)
        )
    )
    assert result.exit_code == 0
    plist_path = agents / f"{LABEL}.plist"
    assert plist_path.read_text() == result.plist
    assert calls == [
        ["launchctl", "bootout", f"gui/4242/{LABEL}"],
        ["launchctl", "bootstrap", "gui/4242", str(plist_path)],
        ["launchctl", "enable", f"gui/4242/{LABEL}"],
        ["launchctl", "kickstart", f"gui/4242/{LABEL}"],
    ]
    record = json.loads((tmp_path / "state" / "installed.json").read_text())
    assert record["revision"] == result.revision
    assert record["host"] == "lab-host"


def test_install_ignores_a_bootout_refusal_and_reports_a_bootstrap_one(
    tmp_path: Path,
) -> None:
    path = launchd_overlay(tmp_path)

    def refusing(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if "bootstrap" in argv:
            return subprocess.CompletedProcess(argv, 5, "", "bootstrap refused")
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = HandlerWorktreeSchedule(
        launchd_directory=tmp_path / "LaunchAgents", uid=4242, runner=refusing
    ).handle(
        ModelWorktreeScheduleRequest(
            mode="install", host="lab-host", overlay_path=str(path)
        )
    )
    assert result.exit_code == 5
    assert "bootstrap refused" in result.stderr
    assert not (tmp_path / "state" / "installed.json").exists()

    def unloaded(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if "bootout" in argv:
            return subprocess.CompletedProcess(argv, 3, "", "job not loaded")
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = HandlerWorktreeSchedule(
        launchd_directory=tmp_path / "LaunchAgents", uid=4242, runner=unloaded
    ).handle(
        ModelWorktreeScheduleRequest(
            mode="install", host="lab-host", overlay_path=str(path)
        )
    )
    assert result.exit_code == 0
    assert (tmp_path / "state" / "installed.json").exists()


def test_launchd_refuses_a_project_under_a_worktree_root(tmp_path: Path) -> None:
    path = worktree_overlay(tmp_path)
    agents = tmp_path / "LaunchAgents"
    rendered = render(tmp_path, path)
    assert rendered.exit_code != 0
    assert "omni_worktrees" in rendered.stderr
    assert f"{LABEL}.plist" in rendered.stderr
    assert rendered.plist == ""

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "", "")

    installed = HandlerWorktreeSchedule(
        launchd_directory=agents, uid=4242, runner=run
    ).handle(
        ModelWorktreeScheduleRequest(
            mode="install", host="lab-host", overlay_path=str(path)
        )
    )
    assert installed.exit_code != 0
    assert "omni_worktrees" in installed.stderr
    assert not agents.exists()
    assert not (tmp_path / "state").exists()


def receipt(host: str, finished_at: datetime) -> str:
    completed = ModelWorktreeReconcileRunCompletedEvent(
        correlation_id=uuid4(),
        host=host,
        started_at=finished_at - timedelta(seconds=1),
        finished_at=finished_at,
        scanned=0,
        removed=0,
        pinned_and_removed=0,
        kept=0,
        needs_human=0,
        failures=0,
        freed_bytes=0,
        needs_human_paths=(),
    )
    return (
        ModelWorktreeReconcileRunResult(
            decided_events=(), completed_event=completed
        ).model_dump_json()
        + "\n"
    )


@pytest.mark.parametrize("case", ["fresh", "stale", "not_loaded"])
def test_launchd_status_requires_a_loaded_job_and_a_fresh_receipt(
    tmp_path: Path, case: str
) -> None:
    path = launchd_overlay(tmp_path)
    calls: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if case == "not_loaded" and "print" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "job not found")
        return subprocess.CompletedProcess(argv, 0, "", "")

    handler = HandlerWorktreeSchedule(
        launchd_directory=tmp_path / "LaunchAgents", uid=4242, runner=run
    )
    installed = handler.handle(
        ModelWorktreeScheduleRequest(
            mode="install", host="lab-host", overlay_path=str(path)
        )
    )
    assert installed.exit_code == 0
    finished = datetime.now(UTC) + timedelta(days=-1 if case == "stale" else 0)
    (tmp_path / "state" / "latest.json").write_text(receipt("lab-host", finished))
    result = handler.handle(
        ModelWorktreeScheduleRequest(
            mode="status", host="lab-host", overlay_path=str(path)
        )
    )
    assert ["launchctl", "print", f"gui/4242/{LABEL}"] in calls
    assert (result.exit_code == 0) is (case == "fresh")


def test_scheduler_for_an_undeclared_host_is_refused(tmp_path: Path) -> None:
    document = yaml.safe_load(overlay(tmp_path).read_text())
    document["schedulers"] = {"other-host": "launchd"}
    with pytest.raises(ValidationError):
        ModelWorktreeScheduleOverlay.model_validate(document)


def test_the_rendered_plist_is_a_unit_root_the_mac_reader_reads(tmp_path: Path) -> None:
    """Seam: what the handler renders is a plist LaunchdUnitRootReader accepts, and it names
    the project path as held by the label, so the tick never removes the tree its unit runs from."""
    path = launchd_overlay(tmp_path)
    agents = tmp_path / "LaunchAgents"
    result = render(tmp_path, path)
    assert result.exit_code == 0
    agents.mkdir(parents=True)
    (agents / f"{LABEL}.plist").write_text(result.plist)
    project = tmp_path / "project"

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 0, f"PID\tStatus\tLabel\n-\t0\t{LABEL}\n", ""
        )

    roots = LaunchdUnitRootReader(directories=[str(agents)], run=run, uid=4242).read()
    assert roots.units_read == 1
    assert roots.naming(str(project)) == LABEL
    assert roots.naming(str(project / "src")) == LABEL
