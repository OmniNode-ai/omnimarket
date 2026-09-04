# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17816: the ``origin/dev`` OCC checkout is shared per SHA, not per run.

``_materialize_occ_dev_worktree`` used to ``mkdtemp`` a fresh
``.occ-dev-wt-<slug>/`` and check out a WHOLE new tree into it on every single
``collect()`` call. The module's own comment at the timeout constant records the
cost: that operation checks out 32,382 files and takes ~34.5 s single-threaded,
uncontended.

So N concurrent verifier runs minted N simultaneous full checkouts from one
clone, all serializing on the same ``.git/index.lock``. Measured on this host
2026-09-03: 266 registrations rising to 292 during a single closeout pass under
70-124 concurrent ``dod_verify`` processes, with EVERY materialisation exceeding
the 300 s ceiling — and, because OMN-16787 correctly made that failure
fail-closed, twelve of thirty-six runs collapsed to one synthetic
``OCC_WORKTREE_UNAVAILABLE`` verdict whose content hash was identical across six
unrelated tickets.

Reaping was tried three times (116 -> 0 on 2026-08-25, 424 -> cleared on
2026-08-28, and again today) and regrew every time, because the producer was
never changed. This module changes the producer: runs resolving the SAME
governance SHA share ONE snapshot, created once under an ``fcntl`` lock and
reused by everyone else.

RED-first. ``TestTheContentionIsReal`` is a characterization control pinning the
git behaviour the ticket rests on; it passes before and after. Every other
assertion fails against the pre-fix collector.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

_SHARED_PREFIX = ".occ-dev-wt-shared-"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _registered(repo: Path) -> list[str]:
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    return [
        line.split(" ", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


def _make_occ_repo(tmp_path: Path) -> Path:
    """A real git repo standing in for the ``onex_change_control`` clone.

    Carries a real ``origin`` remote so ``_refresh_occ_ref`` resolves
    ``origin/dev`` the way production does, rather than short-circuiting on
    ``NOT_APPLICABLE`` and hiding the code path under test.
    """
    upstream = tmp_path / "occ-upstream.git"
    upstream.mkdir()
    _git(upstream, "init", "--bare", "--initial-branch=dev")

    occ = tmp_path / "onex_change_control"
    occ.mkdir()
    _git(occ, "init", "--initial-branch=dev")
    _git(occ, "config", "user.email", "test@omninode.ai")
    _git(occ, "config", "user.name", "test")
    (occ / "contracts").mkdir()
    (occ / "contracts" / "OMN-1.yaml").write_text(
        "ticket_id: OMN-1\n", encoding="utf-8"
    )
    _git(occ, "add", "-A")
    _git(occ, "commit", "-m", "init")
    _git(occ, "remote", "add", "origin", str(upstream))
    _git(occ, "push", "-u", "origin", "dev")
    return occ


def _advance_dev(occ: Path, name: str) -> None:
    """Move ``origin/dev`` forward the way a real merge does."""
    (occ / "contracts" / f"{name}.yaml").write_text(
        f"ticket_id: {name}\n", encoding="utf-8"
    )
    _git(occ, "add", "-A")
    _git(occ, "commit", "-m", name)
    _git(occ, "push", "origin", "dev")


def _collector(occ: Path, omni_home: Path) -> EvidenceCollector:
    os.environ["OMNI_HOME"] = str(omni_home)
    os.environ["ONEX_CC_REPO_PATH"] = str(occ)
    return EvidenceCollector()


def _release(collector: EvidenceCollector) -> None:
    """Drop the run's reader lock on the shared snapshot.

    Deliberately tolerant of the method being absent. Without that, EVERY
    assertion below fails against the pre-fix collector with the same
    ``AttributeError`` — a RED that only proves a helper is new, not that the
    old code minted one full checkout per run. Going through here keeps the RED
    behavioural: the pre-fix failures are the real ones (two registrations where
    one is required, no SHA-keyed path, a snapshot deleted out from under a
    concurrent reader). Post-fix this is a plain passthrough.
    """
    release = getattr(collector, "release_occ_dev_snapshot", None)
    if release is not None:
        release()


def _materialize_in_child(occ: str, omni_home: str, out: Any) -> None:
    """Run one materialisation in a separate PROCESS.

    Threads would not prove anything here: POSIX advisory locks are held per
    OPEN FILE DESCRIPTION within a process, so two threads sharing one
    interpreter do not contend. Real concurrency is N separate
    ``onex skill dod_verify`` processes, so the test has to be process-level.
    """
    os.environ["OMNI_HOME"] = omni_home
    os.environ["ONEX_CC_REPO_PATH"] = occ
    collector = EvidenceCollector()
    try:
        dev_root, created, _outcome, sha = collector._materialize_occ_dev_worktree()
    finally:
        _release(collector)
    out.put((dev_root, str(created) if created else None, sha))


class TestTheContentionIsReal:
    """Characterization control — pins the git facts, passes before and after."""

    @pytest.mark.unit
    def test_two_plain_worktree_adds_produce_two_registrations(
        self, tmp_path: Path
    ) -> None:
        """Baseline: unshared adds grow the registry once per add.

        This is the pre-fix behaviour, asserted directly against git, so the
        rest of this module is measuring a real change and not a fixture.
        """
        occ = _make_occ_repo(tmp_path)
        before = len(_registered(occ))
        for slug in ("wt-one", "wt-two"):
            _git(
                occ,
                "worktree",
                "add",
                "--detach",
                str(tmp_path / slug),
                "origin/dev",
            )
        assert len(_registered(occ)) == before + 2


class TestSharedSnapshotIsCreatedOnce:
    """AC1: N concurrent runs at one SHA share ONE checkout."""

    @pytest.mark.unit
    def test_two_concurrent_runs_add_exactly_one_worktree(self, tmp_path: Path) -> None:
        """The ticket's headline assertion, run as two real processes."""
        occ = _make_occ_repo(tmp_path)
        omni_home = tmp_path / "omni_home"
        omni_home.mkdir()
        before = len(_registered(occ))

        ctx = mp.get_context("spawn")
        queue: Any = ctx.Queue()
        procs = [
            ctx.Process(
                target=_materialize_in_child, args=(str(occ), str(omni_home), queue)
            )
            for _ in range(2)
        ]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=180)
        results = [queue.get(timeout=10) for _ in procs]

        assert all(r[0] is not None for r in results), f"a run failed: {results}"
        assert results[0][0] == results[1][0], (
            "both runs must resolve the SAME snapshot path; got "
            f"{results[0][0]} and {results[1][0]}"
        )
        assert len(_registered(occ)) == before + 1, (
            "two concurrent runs must add exactly ONE registration, not two: "
            f"{_registered(occ)}"
        )

    @pytest.mark.unit
    def test_snapshot_is_keyed_by_the_resolved_sha(self, tmp_path: Path) -> None:
        """The path names the SHA, so a reader can tell what it is looking at."""
        occ = _make_occ_repo(tmp_path)
        omni_home = tmp_path / "omni_home"
        omni_home.mkdir()
        collector = _collector(occ, omni_home)
        try:
            dev_root, _created, _outcome, sha = (
                collector._materialize_occ_dev_worktree()
            )
        finally:
            _release(collector)

        assert dev_root is not None
        assert sha is not None
        assert Path(dev_root).name == f"{_SHARED_PREFIX}{sha[:12]}"
        assert Path(dev_root).parent == omni_home

    @pytest.mark.unit
    def test_a_sequential_second_run_reuses_the_snapshot(self, tmp_path: Path) -> None:
        """Reuse is not only a concurrency property — a later run reuses too."""
        occ = _make_occ_repo(tmp_path)
        omni_home = tmp_path / "omni_home"
        omni_home.mkdir()
        collector = _collector(occ, omni_home)

        first, _c1, _o1, _s1 = collector._materialize_occ_dev_worktree()
        _release(collector)
        after_first = len(_registered(occ))
        second, _c2, _o2, _s2 = collector._materialize_occ_dev_worktree()
        _release(collector)

        assert first == second
        assert len(_registered(occ)) == after_first


class TestTheSnapshotOutlivesTheRun:
    """A shared snapshot must NOT be torn down by whichever run finishes first."""

    @pytest.mark.unit
    def test_created_worktree_is_none_so_the_caller_does_not_remove_it(
        self, tmp_path: Path
    ) -> None:
        """``collect()``'s ``finally`` removes ``created_worktree`` if non-None.

        Returning the shared path there would have run A delete the checkout run
        B is still reading. The shared path is therefore returned as the dev
        root only, with ``created_worktree`` None.
        """
        occ = _make_occ_repo(tmp_path)
        omni_home = tmp_path / "omni_home"
        omni_home.mkdir()
        collector = _collector(occ, omni_home)
        try:
            dev_root, created, _outcome, _sha = (
                collector._materialize_occ_dev_worktree()
            )
        finally:
            _release(collector)

        assert dev_root is not None
        assert created is None
        assert Path(dev_root).is_dir()


class TestSupersededSnapshotsArePruned:
    """AC3: the registry does not grow one entry per distinct dev SHA either."""

    @pytest.mark.unit
    def test_advancing_the_sha_replaces_rather_than_appends(
        self, tmp_path: Path
    ) -> None:
        occ = _make_occ_repo(tmp_path)
        omni_home = tmp_path / "omni_home"
        omni_home.mkdir()
        collector = _collector(occ, omni_home)

        first, _c, _o, sha1 = collector._materialize_occ_dev_worktree()
        _release(collector)
        assert first is not None
        after_first = len(_registered(occ))

        _advance_dev(occ, "OMN-2")

        second, _c2, _o2, sha2 = collector._materialize_occ_dev_worktree()
        _release(collector)

        assert sha2 != sha1
        assert second != first
        assert len(_registered(occ)) == after_first, (
            "advancing the SHA must REPLACE the snapshot, not append one: "
            f"{_registered(occ)}"
        )
        assert not Path(first).exists(), "the superseded snapshot dir must be gone"

    @pytest.mark.unit
    def test_a_snapshot_still_in_use_is_not_reaped(self, tmp_path: Path) -> None:
        """Pruning must never rip the tree out from under a live reader.

        A run that started at the previous SHA still holds a shared lock on its
        snapshot. The prune is conditional on acquiring that lock exclusively,
        so a held snapshot survives — it is reaped by a later pass instead.
        """
        occ = _make_occ_repo(tmp_path)
        omni_home = tmp_path / "omni_home"
        omni_home.mkdir()

        reader = _collector(occ, omni_home)
        held, _c, _o, _s = reader._materialize_occ_dev_worktree()
        assert held is not None
        # Deliberately do NOT release: this reader is still running.

        _advance_dev(occ, "OMN-3")

        writer = _collector(occ, omni_home)
        fresh, _c2, _o2, _s2 = writer._materialize_occ_dev_worktree()
        _release(writer)

        assert fresh != held
        assert Path(held).is_dir(), "a snapshot with a live reader must survive"
        _release(reader)


class TestFailClosedSemanticsAreUnchanged:
    """AC4: OMN-16787's refusal still fires when the snapshot cannot be made."""

    @pytest.mark.unit
    def test_an_unresolvable_governance_ref_still_yields_no_dev_root(
        self, tmp_path: Path
    ) -> None:
        """Negative control. A ref that does not exist must NOT silently pass.

        Without this, a change that made materialisation "always succeed" by
        handing back the clone's own working tree would satisfy every assertion
        above while destroying the property OMN-16787 exists to protect.
        """
        occ = _make_occ_repo(tmp_path)
        omni_home = tmp_path / "omni_home"
        omni_home.mkdir()
        collector = _collector(occ, omni_home)
        collector._occ_governance_ref = "origin/no-such-branch"

        try:
            dev_root, created, _outcome, sha = collector._materialize_occ_dev_worktree()
        finally:
            _release(collector)

        assert dev_root is None, "an unresolvable ref must not yield a dev root"
        assert created is None
        assert sha is None
        assert not list(omni_home.glob(f"{_SHARED_PREFIX}*")), (
            "a failed materialisation must leave no snapshot behind"
        )

    @pytest.mark.unit
    def test_a_corrupt_snapshot_is_rebuilt_not_reused(self, tmp_path: Path) -> None:
        """A half-written snapshot must never be handed out as if it were valid."""
        occ = _make_occ_repo(tmp_path)
        omni_home = tmp_path / "omni_home"
        omni_home.mkdir()
        collector = _collector(occ, omni_home)

        first, _c, _o, _s = collector._materialize_occ_dev_worktree()
        _release(collector)
        assert first is not None
        # Corrupt it exactly the way a killed checkout does: gitfile gone.
        (Path(first) / ".git").unlink()

        second, _c2, _o2, _s2 = collector._materialize_occ_dev_worktree()
        _release(collector)

        assert second == first, "the rebuilt snapshot keeps the same SHA-keyed path"
        assert (Path(second) / ".git").exists(), "it must be a real worktree again"


class TestTheLockIsCrashSafe:
    """AC2: a holder killed mid-add must not wedge the next run."""

    @pytest.mark.unit
    def test_a_leftover_lock_file_does_not_block_a_later_run(
        self, tmp_path: Path
    ) -> None:
        """``fcntl`` locks die with the process; the FILE must not be a barrier.

        A lock implemented as "the lock file exists" would wedge permanently
        after any ``kill -9``. This asserts the file alone is inert.
        """
        occ = _make_occ_repo(tmp_path)
        omni_home = tmp_path / "omni_home"
        omni_home.mkdir()
        collector = _collector(occ, omni_home)

        # Pre-create the creation-mutex file, unheld, as a crash would leave it.
        lock_dir = occ / ".git" / "occ-dev-snapshot-locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "build.lock").write_text("", encoding="utf-8")

        try:
            dev_root, _created, _outcome, _sha = (
                collector._materialize_occ_dev_worktree()
            )
        finally:
            _release(collector)
        assert dev_root is not None, "a stale unheld lock file must not block a run"


class TestNoUntrackedDebrisInOmniHome:
    """The lock files must not reintroduce the OMN-16826 debris class."""

    @pytest.mark.unit
    def test_lock_files_live_under_git_not_beside_the_snapshot(
        self, tmp_path: Path
    ) -> None:
        """A lock file is never deleted, so one accrues per distinct dev SHA.

        The OMN-16826 .gitignore rule is ``.occ-dev-wt-*/`` — trailing slash, so
        it matches DIRECTORIES only. Lock files placed beside the snapshots in
        ``omni_home`` would be permanently untracked ``??`` entries that one
        ``git add -A`` commits, which is precisely the debris that ticket
        closed. They belong under the clone's ``.git/``, where nothing can add
        them.
        """
        occ = _make_occ_repo(tmp_path)
        omni_home = tmp_path / "omni_home"
        omni_home.mkdir()
        collector = _collector(occ, omni_home)
        try:
            dev_root, _created, _outcome, _sha = (
                collector._materialize_occ_dev_worktree()
            )
        finally:
            _release(collector)
        assert dev_root is not None

        stray = [p.name for p in omni_home.iterdir() if p.is_file()]
        assert stray == [], f"lock files leaked into omni_home: {stray}"
        assert list((occ / ".git" / "occ-dev-snapshot-locks").glob("*.lock")), (
            "the locks must exist somewhere — under .git/"
        )
