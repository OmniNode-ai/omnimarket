# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A tree a loaded launchd job names is kept by the worktree reconcile (OMN-23323).

Recorded case: on 2026-11-09 the factory floor publisher (a launchd job on the operator's Mac) read its overlay root from
omni_worktrees/OMN-23081/overlays; the content-keyed removal took the tree and the published floor lost every owner ticket
added that day. The reconcile node's unit reader asked ``systemctl``, which a Mac does not have, so it could never name
such a tree. Each test runs the whole reconcile on a real squash-merged worktree; the failing control is the same tree
with a unit directory that names nothing, which is removed.
"""

from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_launchd_units import (
    LaunchdUnitRootReader,
    print_paths,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_units import (
    UnitSnapshotError,
)
from tests.test_worktree_reconcile_effect import (
    FakeMergedPrReader,
    probe_for,
    run_with,
    squash_merged_then_moved_on,
)

pytestmark = pytest.mark.unit

LABEL = "ai.example.factory-floor-publish"


class _Launchctl:
    """A scripted launchctl: the loaded list, a printed job per label, or a failing exit."""

    def __init__(
        self,
        loaded: Sequence[str] = (LABEL,),
        printed: dict[str, str] | None = None,
        *,
        list_rc: int = 0,
    ) -> None:
        self.loaded = tuple(loaded)
        self.printed = printed or {}
        self.list_rc = list_rc
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        if command[1] == "list":
            rows = "PID\tStatus\tLabel\n" + "".join(f"-\t0\t{n}\n" for n in self.loaded)
            return subprocess.CompletedProcess(list(command), self.list_rc, rows, "")
        label = command[2].rsplit("/", 1)[-1]
        if label not in self.printed:
            return subprocess.CompletedProcess(list(command), 113, "", "no such job")
        return subprocess.CompletedProcess(list(command), 0, self.printed[label], "")


def install_plist(directory: Path, roots: str, label: str = LABEL) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    document = {
        "Label": label,
        "ProgramArguments": ["/usr/bin/uv", "run", "python", "-m", "publisher"],
        "EnvironmentVariables": {"ONEX_SKILL_OVERLAY_ROOTS": roots},
    }
    (directory / f"{label}.plist").write_bytes(plistlib.dumps(document))


def reader_for(directory: Path, launchctl: _Launchctl) -> LaunchdUnitRootReader:
    return LaunchdUnitRootReader(directories=(str(directory),), run=launchctl, uid=501)


def test_worktree_triage_unit_reference_a_plist_environment_path_keeps_the_tree_and_its_absence_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Golden chain event with its failing control: the decided event carries live_unit."""
    root, path, head = squash_merged_then_moved_on(tmp_path)
    reader = FakeMergedPrReader(frozenset({head}))
    agents = tmp_path / "LaunchAgents"
    install_plist(agents, f"{path.resolve()}/overlays:/elsewhere/plugin/overlays")
    result = run_with(
        probe_for(reader, monkeypatch, units=reader_for(agents, _Launchctl())),
        root,
        monkeypatch,
    )
    row = result.decided_events[0]
    assert row.facts.live_unit == LABEL
    assert row.decision.reasons == ("live_unit",)
    assert row.outcome == "kept"
    assert path.exists()
    # The failing control: the same tree and evidence with no job naming it is removed.
    empty = tmp_path / "EmptyAgents"
    empty.mkdir()
    control = run_with(
        probe_for(
            reader,
            monkeypatch,
            units=reader_for(empty, _Launchctl(loaded=("x.y",), printed={"x.y": ""})),
        ),
        root,
        monkeypatch,
    )
    assert control.decided_events[0].outcome == "removed"
    assert not path.exists()


def test_worktree_triage_unit_reference_a_loaded_job_kept_outside_the_agent_directories_keeps_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, path, head = squash_merged_then_moved_on(tmp_path)
    reader = FakeMergedPrReader(frozenset({head}))
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    printed = f"{LABEL} = {{\n\tprogram = /usr/bin/uv\n\tworking directory = {path.resolve()}\n}}\n"
    launchctl = _Launchctl(printed={LABEL: printed})
    result = run_with(
        probe_for(reader, monkeypatch, units=reader_for(agents, launchctl)),
        root,
        monkeypatch,
    )
    assert result.decided_events[0].facts.live_unit == LABEL
    assert result.decided_events[0].outcome == "kept"
    assert path.exists()
    assert ["launchctl", "print", f"gui/501/{LABEL}"] in launchctl.commands


def test_worktree_triage_unit_reference_a_failing_launchctl_keeps_every_tree_and_counts_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error chain event: no tree is judged free of jobs launchd cannot be asked about."""
    root, path, head = squash_merged_then_moved_on(tmp_path)
    reader = FakeMergedPrReader(frozenset({head}))
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    result = run_with(
        probe_for(reader, monkeypatch, units=reader_for(agents, _Launchctl(list_rc=1))),
        root,
        monkeypatch,
    )
    row = result.decided_events[0]
    assert "unit_snapshot_failed" in row.facts.probe_errors
    assert row.outcome == "kept"
    assert path.exists()
    assert result.completed_event.failures >= 1


def test_worktree_triage_unit_reference_an_unparseable_plist_raises_not_an_empty_answer(
    tmp_path: Path,
) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / f"{LABEL}.plist").write_text("not a property list", encoding="utf-8")
    with pytest.raises(UnitSnapshotError):
        reader_for(agents, _Launchctl()).read()


def test_worktree_triage_unit_reference_a_loaded_job_launchd_cannot_print_raises(
    tmp_path: Path,
) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    with pytest.raises(UnitSnapshotError):
        reader_for(agents, _Launchctl(printed={})).read()


def test_worktree_triage_unit_reference_system_jobs_are_not_printed_and_a_sibling_is_not_named(
    tmp_path: Path,
) -> None:
    agents = tmp_path / "LaunchAgents"
    tree = tmp_path / "omni_worktrees" / "OMN-1" / "overlays"
    install_plist(agents, f"{tree}-other/overlays")
    launchctl = _Launchctl(loaded=(LABEL, "com.apple.Finder"))
    roots = reader_for(agents, launchctl).read()
    assert roots.naming(str(tree)) == ""
    assert roots.naming(f"{tree}-other") == LABEL
    assert not any(c[1] == "print" for c in launchctl.commands)


def test_worktree_triage_unit_reference_print_paths_reads_every_absolute_path() -> None:
    text = "x = {\n\tprogram = /usr/bin/uv\n\tenvironment = {\n\t\tA => /a/b:/c/d\n\t}\n}\n"
    assert print_paths(text) == frozenset({"/usr/bin/uv", "/a/b", "/c/d"})
