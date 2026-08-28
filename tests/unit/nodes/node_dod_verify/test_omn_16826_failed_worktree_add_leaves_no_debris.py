# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16826 AC(b): a failed OCC ``worktree add`` must not leave locked debris.

``_materialize_occ_dev_worktree`` mkdtemps ``.occ-dev-wt-<slug>/`` under
``OMNI_HOME`` and runs ``git worktree add --detach`` into it. ``git worktree
add`` marks the new registration ``locked`` with the reason ``initializing``
for the duration of the checkout and unlocks it on success. When the add dies
partway — the 300 s ceiling trips on a 32k-file cold checkout under a 5-way
parallel sweep, or the ref does not resolve — the process is killed with the
lock still set.

Both failure paths then called ``shutil.rmtree(tmp)`` and returned. That deletes
the DIRECTORY but not the REGISTRATION, and the registration is still locked, so
the one command that would reap it is inert:

* ``git worktree prune`` **silently skips locked entries** — a no-op that
  reports success;
* ``git worktree remove`` refuses, because a half-initialised directory with no
  ``.git`` file is not a valid worktree.

Measured consequence on this machine (2026-08-28): 58 ``.occ-dev-wt-*``
directories in the ``omni_home`` root, 19 of them registered-and-locked
half-adds, against ``onex_change_control`` carrying 414 worktree registrations
of which the bare prune could reach none. A one-shot manual cleanup took the
registrations 427 -> 408 and they regrew to 418 within the same session, because
the producer was never fixed. This module is the producer-side fix: the failure
paths unlock what they locked, so prune reaches it.

RED-first. ``TestTheDebrisClassIsReal`` is a characterization control that pins
the git behaviour this ticket rests on and passes before and after the fix;
every other assertion here fails against the pre-fix collector.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

# git's own reason string for the lock it holds across `worktree add`.
_INITIALIZING = "initializing"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _registered_worktrees(repo: Path) -> list[str]:
    """Paths git currently considers registered worktrees of ``repo``."""
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    return [
        line.split(" ", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


def _make_occ_repo(tmp_path: Path) -> Path:
    """A real git repo standing in for the ``onex_change_control`` clone."""
    occ = tmp_path / "onex_change_control"
    occ.mkdir()
    _git(occ, "init", "--initial-branch=dev")
    _git(occ, "config", "user.email", "test@omninode.ai")
    _git(occ, "config", "user.name", "test")
    (occ / "README.md").write_text("occ\n", encoding="utf-8")
    _git(occ, "add", "README.md")
    _git(occ, "commit", "-m", "init")
    return occ


def _make_locked_half_add(occ: Path, parent: Path, slug: str) -> Path:
    """Reproduce the observed debris with real git, not a hand-built fixture.

    The observed shape is: a ``.occ-dev-wt-*`` directory holding only ``drift/``
    and NO ``.git`` file, plus a registration still marked ``locked
    initializing``. It is produced here by adding a real worktree, locking it
    with git's own reason, and then removing the gitfile the way a killed
    checkout leaves things.
    """
    tmp = parent / f".occ-dev-wt-{slug}"
    _git(occ, "worktree", "add", "--detach", str(tmp), "HEAD")
    _git(occ, "worktree", "lock", "--reason", _INITIALIZING, str(tmp))
    (tmp / ".git").unlink()
    for child in tmp.iterdir():
        if child.is_dir():
            continue
        child.unlink()
    (tmp / "drift").mkdir(exist_ok=True)
    return tmp


def _collector_for(occ: Path, parent: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(occ))
    monkeypatch.setenv("OMNI_HOME", str(parent))
    return EvidenceCollector()


@pytest.mark.unit
class TestTheDebrisClassIsReal:
    """Characterization control: pins the git behaviour the ticket rests on.

    Passes before and after the fix. Without it, a green cleanup test proves
    nothing — it could be green because prune already worked.
    """

    def test_bare_prune_cannot_reach_a_locked_half_add(self, tmp_path: Path) -> None:
        occ = _make_occ_repo(tmp_path)
        parent = tmp_path / "omni_home"
        parent.mkdir()
        tmp = _make_locked_half_add(occ, parent, "locked01")

        # The directory is gone exactly as `shutil.rmtree(tmp)` left it pre-fix.
        subprocess.run(["rm", "-rf", str(tmp)], check=True)

        pruned = _git(occ, "worktree", "prune", "-v").stdout
        assert pruned.strip() == "", (
            "control invalid: bare `git worktree prune -v` reported work, so the "
            "locked-entry skip this ticket is about did not reproduce"
        )
        assert str(tmp) in _registered_worktrees(occ)


@pytest.mark.unit
class TestFailedAddCleanupClearsTheRegistration:
    """AC(b), proved by execution against real git."""

    def test_cleanup_unlocks_removes_and_prunes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        occ = _make_occ_repo(tmp_path)
        parent = tmp_path / "omni_home"
        parent.mkdir()
        tmp = _make_locked_half_add(occ, parent, "locked02")
        collector = _collector_for(occ, parent, monkeypatch)

        collector._cleanup_failed_occ_worktree_add(occ, tmp)

        assert not tmp.exists()
        assert str(tmp) not in _registered_worktrees(occ)

    def test_cleanup_converges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second bare prune finds nothing left — the state is settled, not
        merely one step further along."""
        occ = _make_occ_repo(tmp_path)
        parent = tmp_path / "omni_home"
        parent.mkdir()
        tmp = _make_locked_half_add(occ, parent, "locked03")
        collector = _collector_for(occ, parent, monkeypatch)

        collector._cleanup_failed_occ_worktree_add(occ, tmp)

        assert _git(occ, "worktree", "prune", "-v").stdout.strip() == ""

    def test_cleanup_leaves_a_healthy_sibling_worktree_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Permanent negative control: the cleanup must reap its own failed add,
        not become a blanket reaper of the shared OCC clone's registrations."""
        occ = _make_occ_repo(tmp_path)
        parent = tmp_path / "omni_home"
        parent.mkdir()
        healthy = parent / ".occ-dev-wt-healthy"
        _git(occ, "worktree", "add", "--detach", str(healthy), "HEAD")
        tmp = _make_locked_half_add(occ, parent, "locked04")
        collector = _collector_for(occ, parent, monkeypatch)

        collector._cleanup_failed_occ_worktree_add(occ, tmp)

        assert healthy.is_dir()
        assert str(healthy) in _registered_worktrees(occ)


@pytest.mark.unit
class TestMaterializeFailurePathsLeaveNothingBehind:
    """The two live failure paths, end to end through the real method."""

    def test_nonzero_add_leaves_no_directory_and_no_registration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real failing add: the governance ref does not resolve."""
        occ = _make_occ_repo(tmp_path)
        parent = tmp_path / "omni_home"
        parent.mkdir()
        monkeypatch.setenv("OCC_GOVERNANCE_REF", "no-such-ref-omn-16826")
        collector = _collector_for(occ, parent, monkeypatch)
        before = set(_registered_worktrees(occ))

        path_str, path, _outcome, sha = collector._materialize_occ_dev_worktree()

        assert (path_str, path, sha) == (None, None, None)
        assert list(parent.glob(".occ-dev-wt-*")) == []
        assert set(_registered_worktrees(occ)) == before

    def test_timed_out_add_leaves_no_directory_and_no_registration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 300 s-ceiling path — git killed mid-checkout, lock still held.

        ``subprocess.run`` is intercepted only for the ``worktree add`` command:
        it performs the real add, locks it the way git does during init, strips
        the gitfile, and then raises the timeout the ceiling would have raised.
        Everything else runs for real.
        """
        occ = _make_occ_repo(tmp_path)
        parent = tmp_path / "omni_home"
        parent.mkdir()
        collector = _collector_for(occ, parent, monkeypatch)
        before = set(_registered_worktrees(occ))

        real_run = subprocess.run

        def fake_run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(cmd, list) and cmd[3:5] == ["worktree", "add"]:
                target = Path(cmd[-2])
                real_run(
                    [
                        "git",
                        "-C",
                        str(occ),
                        "worktree",
                        "add",
                        "--detach",
                        str(target),
                        "HEAD",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                real_run(
                    [
                        "git",
                        "-C",
                        str(occ),
                        "worktree",
                        "lock",
                        "--reason",
                        _INITIALIZING,
                        str(target),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                (target / ".git").unlink()
                raise subprocess.TimeoutExpired(cmd, 300.0)
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)

        path_str, path, _outcome, sha = collector._materialize_occ_dev_worktree()

        # No ``monkeypatch.undo()`` here, deliberately. ``monkeypatch`` is ONE
        # function-scoped instance shared with the autouse conftest fixture that
        # strips ``GIT_DIR``/``GIT_WORK_TREE``/``GIT_INDEX_FILE`` — git exports
        # those when it invokes a pre-push hook, and they override ``git -C``.
        # Undoing here restores them, so every assertion below would read the
        # INVOKING repository's worktree list instead of the fixture's. (Caught
        # exactly that way: green in isolation, red under `git push`.) The fake
        # intercepts only `worktree add` and delegates everything else, so
        # leaving it installed is harmless.
        assert (path_str, path, sha) == (None, None, None)
        assert list(parent.glob(".occ-dev-wt-*")) == []
        assert set(_registered_worktrees(occ)) == before


@pytest.mark.unit
class TestSuccessPathRemovalAlsoClearsALockedEntry:
    """``_remove_occ_dev_worktree`` fell into the same trap from the other side:
    ``git worktree remove`` refuses a LOCKED worktree even with ``--force``, and
    its fallback was a bare prune, which skips locked entries. The teardown of a
    worktree whose add never finished unlocking therefore left the identical
    debris."""

    def test_remove_clears_a_locked_registration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        occ = _make_occ_repo(tmp_path)
        parent = tmp_path / "omni_home"
        parent.mkdir()
        tmp = _make_locked_half_add(occ, parent, "locked05")
        collector = _collector_for(occ, parent, monkeypatch)

        collector._remove_occ_dev_worktree(tmp)

        assert not tmp.exists()
        assert str(tmp) not in _registered_worktrees(occ)
