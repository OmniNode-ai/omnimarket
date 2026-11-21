# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A launchd definition the run user cannot read or parse is declared, or the snapshot fails.

Recorded case: on the operator's Mac /Library/LaunchDaemons/com.gopro.GoProWebcamDaemon.plist is
malformed XML (``plistlib.loads`` raises ``xml.parsers.expat.ExpatError``, which the reader's
except clause never named) and /Library/LaunchDaemons/com.nordvpn.macos.helper.plist is root-only
(``PermissionError`` on ``read_bytes``); either failed the whole snapshot, so every tree was kept
as facts_unreadable and counted as a failure on every tick. A definition the host overlay declares
with the paths it holds is read as those paths, exactly like an unreadable systemd EnvironmentFile;
one it does not declare still fails the snapshot. Each test's failing control is the same
definition without a declaration.
"""

from __future__ import annotations

import plistlib
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.model_worktree_reconcile_policy import (
    ModelWorktreeReconcilePolicy,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_launchd_units import (
    LaunchdUnitRootReader,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_units import (
    UnitSnapshotError,
)
from tests.test_worktree_triage_unit_reference import _Launchctl

pytestmark = pytest.mark.unit

LABEL = "com.example.broken"
MALFORMED_PLIST = "<?xml version=1.0 encoding=UTF-8?><plist>"
#: A loaded system job: the reader skips it, so no ``launchctl print`` is scripted.
SYSTEM_LABEL = "com.apple.mixed"


def install_malformed_plist(directory: Path, label: str = LABEL) -> Path:
    """A truncated XML definition: ``plistlib.loads`` raises ``xml.parsers.expat.ExpatError``."""
    directory.mkdir(parents=True, exist_ok=True)
    entry = directory / f"{label}.plist"
    entry.write_text(MALFORMED_PLIST, encoding="utf-8")
    return entry


def install_readable_plist(directory: Path, label: str = LABEL) -> Path:
    """A well-formed definition whose ``read_bytes`` a test denies (a root-only file)."""
    directory.mkdir(parents=True, exist_ok=True)
    entry = directory / f"{label}.plist"
    entry.write_bytes(plistlib.dumps({"Label": label, "Program": "/usr/bin/true"}))
    return entry


def deny_read_bytes(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """Every ``Path.read_bytes`` of ``target`` fails with ``PermissionError`` (no chmod)."""

    def guarded(self: Path) -> bytes:
        if self == target:
            raise PermissionError(13, "Operation not permitted")
        return Path.read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", guarded)


def reader_for(
    directory: Path,
    launchctl: _Launchctl,
    declared: Mapping[str, Sequence[str]] | None = None,
) -> LaunchdUnitRootReader:
    return LaunchdUnitRootReader(
        directories=(str(directory),),
        run=launchctl,
        uid=501,
        declared_definitions=declared,
    )


def test_worktree_reconcile_launchd_a_malformed_plist_undeclared_fails_the_snapshot(
    tmp_path: Path,
) -> None:
    """Failing control: the ExpatError the except clause never named must not leak raw."""
    daemons = tmp_path / "LaunchDaemons"
    entry = install_malformed_plist(daemons)
    with pytest.raises(UnitSnapshotError) as raised:
        reader_for(daemons, _Launchctl()).read()
    assert str(entry) in str(raised.value)
    assert "ExpatError" in str(raised.value)


def test_worktree_reconcile_launchd_a_malformed_plist_declared_to_hold_nothing_is_read(
    tmp_path: Path,
) -> None:
    """The same definition with an empty declaration reads as a unit that names no path."""
    daemons = tmp_path / "LaunchDaemons"
    entry = install_malformed_plist(daemons)
    tree = tmp_path / "omni_worktrees" / "OMN-1"
    roots = reader_for(
        daemons,
        _Launchctl(loaded=(SYSTEM_LABEL,)),
        declared={str(entry): ()},
    ).read()
    assert roots.units_read == 1
    assert roots.naming(str(tree)) == ""


def test_worktree_reconcile_launchd_a_root_only_plist_is_declared_or_fails_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing control: a definition whose read is denied fails the snapshot undeclared."""
    daemons = tmp_path / "LaunchDaemons"
    entry = install_readable_plist(daemons)
    deny_read_bytes(monkeypatch, entry)
    with pytest.raises(UnitSnapshotError) as raised:
        reader_for(daemons, _Launchctl()).read()
    assert "PermissionError" in str(raised.value)
    roots = reader_for(
        daemons,
        _Launchctl(loaded=(SYSTEM_LABEL,)),
        declared={str(entry): ()},
    ).read()
    assert roots.units_read == 1


def test_worktree_reconcile_launchd_a_declared_definition_holds_its_path_in_the_roots(
    tmp_path: Path,
) -> None:
    """The declared paths land in the live-path set, so a tree under one would be kept."""
    daemons = tmp_path / "LaunchDaemons"
    entry = install_malformed_plist(daemons)
    tree = tmp_path / "omni_worktrees" / "OMN-1" / "overlays"
    roots = reader_for(
        daemons,
        _Launchctl(loaded=(SYSTEM_LABEL,)),
        declared={str(entry): (str(tree),)},
    ).read()
    assert roots.naming(str(tree)) == LABEL
    assert roots.naming(f"{tree}-other") == ""


def test_worktree_reconcile_policy_declared_launchd_definitions_must_be_absolute() -> (
    None
):
    """The validator names the field and refuses a relative key or a relative path."""
    assert ModelWorktreeReconcilePolicy().unreadable_launchd_definitions == {}
    absolute = "/Library/LaunchDaemons/com.example.broken.plist"
    declared = ModelWorktreeReconcilePolicy(
        unreadable_launchd_definitions={absolute: ("/srv/onex/worktrees",)}
    )
    assert declared.unreadable_launchd_definitions == {
        absolute: ("/srv/onex/worktrees",)
    }
    with pytest.raises(ValidationError) as key:
        ModelWorktreeReconcilePolicy(
            unreadable_launchd_definitions={"Library/LaunchDaemons/x.plist": ()}
        )
    assert "unreadable_launchd_definitions key is not an absolute path" in str(
        key.value
    )
    with pytest.raises(ValidationError) as path:
        ModelWorktreeReconcilePolicy(
            unreadable_launchd_definitions={absolute: ("worktrees/overlays",)}
        )
    assert "holds a path that is not absolute" in str(path.value)
