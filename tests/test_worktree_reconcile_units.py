# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A tree an installed unit runs from is kept, and an unreadable unit snapshot keeps every tree (OMN-22736).

Recorded case: on 2026-11-06 the h053 worktree reconcile removed omni_worktrees/OMN-22643/omnimarket
(decision remove, reason content_merged) while onex-reading-hourly.service named that tree's venv in
ExecStart and in two Environment lines; the 18:15Z tick then failed every piece.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.handlers.rules_unit_roots import (
    UnitRoots,
    split_systemctl_cat,
    unit_paths,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_units import (
    SystemdUnitRootReader,
    UnitSnapshotError,
)
from tests.test_worktree_reconcile_effect import (
    FakeMergedPrReader,
    StaticUnits,
    probe_for,
    run_with,
    squash_merged_then_moved_on,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "worktree_reconcile"
    / "onex-reading-hourly.service.18-15Z.txt"
)
REMOVED_TREE = "/srv/registry/omni_worktrees/OMN-22643/omnimarket"
UNIT = "onex-reading-hourly.service"


def recorded_roots() -> UnitRoots:
    units = split_systemctl_cat(FIXTURE.read_text(encoding="utf-8"))
    return UnitRoots.from_paths(
        {name: unit_paths(text) for name, text in units.items()}
    )


def test_the_recorded_unit_names_the_tree_the_reconcile_removed() -> None:
    roots = recorded_roots()
    assert roots.naming(REMOVED_TREE) == UNIT
    # A tree a unit names a path inside counts; a part of it no unit names does not, nor does a
    # sibling tree.
    assert roots.naming(f"{REMOVED_TREE}/.venv") == UNIT
    assert roots.naming(f"{REMOVED_TREE}/src") == ""
    assert roots.naming("/srv/registry/omni_worktrees/OMN-22644/omnimarket") == ""


def test_the_ticket_directory_above_the_named_tree_is_named_and_the_clone_root_is_a_path_too() -> (
    None
):
    assert recorded_roots().naming("/srv/registry/omni_worktrees/OMN-22643") == UNIT
    assert "/srv/registry" in unit_paths(FIXTURE.read_text(encoding="utf-8"))


def test_comments_and_settings_that_hold_no_path_name_nothing() -> None:
    text = (
        "[Service]\n"
        "# ExecStart=/srv/registry/omni_worktrees/OMN-1/x/bin/python\n"
        "Description=runs /srv/registry/omni_worktrees/OMN-2/x\n"
        "SyslogIdentifier=/srv/registry/omni_worktrees/OMN-3/x\n"
        "ExecStart=/bin/true\n"
    )
    assert unit_paths(text) == frozenset({"/bin/true"})


def test_a_tree_a_unit_names_is_kept_where_the_same_tree_unnamed_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Golden chain event with its failing control: the decided event carries live_unit."""
    root, path, head = squash_merged_then_moved_on(tmp_path)
    reader = FakeMergedPrReader(frozenset({head}))
    named = UnitRoots.from_paths(
        {"onex-test.service": [str(path.resolve() / ".venv/bin/python")]}
    )
    result = run_with(
        probe_for(reader, monkeypatch, units=StaticUnits(named)), root, monkeypatch
    )
    row = result.decided_events[0]
    assert row.facts.live_unit == "onex-test.service"
    assert row.decision.reasons == ("live_unit",)
    assert row.outcome == "kept"
    assert path.exists()
    assert result.completed_event.removed == 0
    # The failing control: the same tree and evidence with no unit naming it is removed.
    control = run_with(
        probe_for(reader, monkeypatch, units=StaticUnits()), root, monkeypatch
    )
    assert control.decided_events[0].outcome == "removed"
    assert not path.exists()


class _Unreadable:
    def read(self) -> UnitRoots:
        raise UnitSnapshotError("systemctl --user list-unit-files exited 1")


def test_an_unreadable_unit_snapshot_keeps_every_tree_and_counts_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error chain event: no tree is judged free of units it cannot read."""
    root, path, head = squash_merged_then_moved_on(tmp_path)
    reader = FakeMergedPrReader(frozenset({head}))
    result = run_with(
        probe_for(reader, monkeypatch, units=_Unreadable()), root, monkeypatch
    )
    row = result.decided_events[0]
    assert "unit_snapshot_failed" in row.facts.probe_errors
    assert row.decision.reasons == ("facts_unreadable",)
    assert row.outcome == "kept"
    assert path.exists()
    assert result.completed_event.failures >= 1


class _NamesAfterDiscovery:
    """Answers with nothing at discovery and names the tree on every later read."""

    def __init__(self, tree: str) -> None:
        self.tree = tree
        self.reads = 0

    def read(self) -> UnitRoots:
        self.reads += 1
        if self.reads == 1:
            return UnitRoots()
        return UnitRoots.from_paths({"onex-late.service": [self.tree]})


def test_a_unit_installed_between_discovery_and_removal_stops_the_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, path, head = squash_merged_then_moved_on(tmp_path)
    reader = FakeMergedPrReader(frozenset({head}))
    units = _NamesAfterDiscovery(str(path.resolve()))
    result = run_with(probe_for(reader, monkeypatch, units=units), root, monkeypatch)
    row = result.decided_events[0]
    assert row.decision.reasons == ("revalidation_refused",)
    assert row.outcome == "kept"
    assert path.exists()
    assert units.reads >= 2


class _Systemctl:
    """A scripted systemctl: the listing and the cat text per scope, or a failing exit."""

    def __init__(self, *, listing_rc: int = 0, text: str | None = None) -> None:
        self.listing_rc = listing_rc
        self.text = text if text is not None else FIXTURE.read_text(encoding="utf-8")
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        verb = command[2]
        if verb == "list-unit-files":
            out = f"{UNIT} static -\n" if command[1] == "--user" else ""
            return subprocess.CompletedProcess(list(command), self.listing_rc, out, "")
        return subprocess.CompletedProcess(list(command), 0, self.text, "")


def test_the_systemd_reader_asks_both_managers_and_names_the_recorded_tree() -> None:
    systemctl = _Systemctl()
    roots = SystemdUnitRootReader(run=systemctl).read()
    assert roots.naming(REMOVED_TREE) == UNIT
    asked = {(c[1], c[2]) for c in systemctl.commands}
    assert ("--user", "cat") in asked
    assert ("--system", "list-unit-files") in asked


def test_a_failing_systemctl_raises_instead_of_answering_with_nothing() -> None:
    with pytest.raises(UnitSnapshotError):
        SystemdUnitRootReader(run=_Systemctl(listing_rc=1)).read()


def test_a_required_environment_file_that_is_missing_raises_and_an_optional_one_does_not(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "absent.env"
    base = "[Service]\nExecStart=/bin/true\n"
    with pytest.raises(UnitSnapshotError):
        SystemdUnitRootReader(
            run=_Systemctl(text=f"# /etc/x/{UNIT}\n{base}EnvironmentFile={missing}\n")
        ).read()
    SystemdUnitRootReader(
        run=_Systemctl(text=f"# /etc/x/{UNIT}\n{base}EnvironmentFile=-{missing}\n")
    ).read()


def test_an_environment_file_that_names_a_tree_counts(tmp_path: Path) -> None:
    env = tmp_path / "host.env"
    env.write_text(f"OMNIBASE_PATH={REMOVED_TREE}\n", encoding="utf-8")
    text = f"# /etc/x/{UNIT}\n[Service]\nExecStart=/bin/true\nEnvironmentFile={env}\n"
    roots = SystemdUnitRootReader(run=_Systemctl(text=text)).read()
    assert roots.naming(REMOVED_TREE) == UNIT


def _env_unit_text(env: str) -> str:
    return f"# /etc/x/{UNIT}\n[Service]\nExecStart=/bin/true\nEnvironmentFile={env}\n"


def test_declared_unreadable_environment_file_joins_the_live_set(
    tmp_path: Path,
) -> None:
    env = str(tmp_path / "secret.env")
    reader = SystemdUnitRootReader(
        run=_Systemctl(text=_env_unit_text(env)),
        declared_environment_files={env: (REMOVED_TREE,)},
        read_file=lambda path: (_ for _ in ()).throw(PermissionError("denied")),
    )
    assert reader.read().naming(REMOVED_TREE) == UNIT


def test_declared_unreadable_environment_file_with_no_paths_names_nothing(
    tmp_path: Path,
) -> None:
    env = str(tmp_path / "secret.env")
    reader = SystemdUnitRootReader(
        run=_Systemctl(text=_env_unit_text(env)),
        declared_environment_files={env: ()},
        read_file=lambda path: (_ for _ in ()).throw(PermissionError("denied")),
    )
    roots = reader.read()
    assert roots.naming(REMOVED_TREE) == ""


def test_undeclared_unreadable_environment_file_still_raises(tmp_path: Path) -> None:
    env = str(tmp_path / "secret.env")
    text = _env_unit_text(env)
    with pytest.raises(UnitSnapshotError):
        SystemdUnitRootReader(
            run=_Systemctl(text=text),
            read_file=lambda path: (_ for _ in ()).throw(PermissionError("denied")),
        ).read()
    with pytest.raises(UnitSnapshotError):
        SystemdUnitRootReader(
            run=_Systemctl(text=text),
            declared_environment_files={"/etc/other.env": (REMOVED_TREE,)},
            read_file=lambda path: (_ for _ in ()).throw(PermissionError("denied")),
        ).read()


def test_declared_value_does_not_cover_a_missing_required_file(tmp_path: Path) -> None:
    env = str(tmp_path / "secret.env")
    with pytest.raises(UnitSnapshotError):
        SystemdUnitRootReader(
            run=_Systemctl(text=_env_unit_text(env)),
            declared_environment_files={env: (REMOVED_TREE,)},
            read_file=lambda path: (_ for _ in ()).throw(FileNotFoundError("missing")),
        ).read()


def test_declared_value_is_ignored_when_the_file_is_readable(tmp_path: Path) -> None:
    env = str(tmp_path / "secret.env")
    roots = SystemdUnitRootReader(
        run=_Systemctl(text=_env_unit_text(env)),
        declared_environment_files={env: ()},
        read_file=lambda path: f"OMNIBASE_PATH={REMOVED_TREE}\n",
    ).read()
    assert roots.naming(REMOVED_TREE) == UNIT


def test_declared_unreadable_environment_file_run_chain_golden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, path, head = squash_merged_then_moved_on(tmp_path)
    reader = FakeMergedPrReader(frozenset({head}))
    env = str(tmp_path / "secret.env")
    units = SystemdUnitRootReader(
        run=_Systemctl(text=_env_unit_text(env)),
        declared_environment_files={env: (str(path.resolve()),)},
        read_file=lambda p: (_ for _ in ()).throw(PermissionError("denied")),
    )
    result = run_with(probe_for(reader, monkeypatch, units=units), root, monkeypatch)
    row = result.decided_events[0]
    assert "unit_snapshot_failed" not in row.facts.probe_errors
    assert result.completed_event.failures == 0


def test_undeclared_unreadable_environment_file_run_chain_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, path, head = squash_merged_then_moved_on(tmp_path)
    reader = FakeMergedPrReader(frozenset({head}))
    env = str(tmp_path / "secret.env")
    units = SystemdUnitRootReader(
        run=_Systemctl(text=_env_unit_text(env)),
        read_file=lambda p: (_ for _ in ()).throw(PermissionError("denied")),
    )
    result = run_with(probe_for(reader, monkeypatch, units=units), root, monkeypatch)
    row = result.decided_events[0]
    assert "unit_snapshot_failed" in row.facts.probe_errors
    assert row.outcome == "kept"
    assert path.exists()
    assert result.completed_event.failures >= 1


def test_unreadable_environment_files_must_be_absolute_paths() -> None:
    from uuid import uuid4

    from pydantic import ValidationError

    from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileCommand

    with pytest.raises(ValidationError):
        ModelWorktreeReconcileCommand(
            host="h",
            roots=("/r",),
            correlation_id=uuid4(),
            unreadable_environment_files={"relative/env": ("/a",)},
        )
    with pytest.raises(ValidationError):
        ModelWorktreeReconcileCommand(
            host="h",
            roots=("/r",),
            correlation_id=uuid4(),
            unreadable_environment_files={"/env": ("relative/a",)},
        )
