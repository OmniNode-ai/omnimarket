# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A canonical clone refused past a declared age is refused again, never reset, while it holds local work (OMN-21821, OMN-19998).

The planted clones are the two real cases of 2026-10-31: a host's omniclaude held back by staged
source files that had since merged upstream, and a host's omnidash held back by a stale generated
file. Until OMN-19998 the tick reset both and dropped what they held; now each is REFUSED, kept byte
for byte, and alarmed. The age is the contract's, replaced by the overlay variable. By default the test drives a fake
engine and a fake converge script; with ``ONEX_TEST_OMNICLAUDE_ROOT`` naming a checkout of the
sync engine's repository it drives the real engine and the real script instead.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env
from pydantic import ValidationError

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    EnumDeliveryOutcome,
    EnumDeliveryStep,
    ModelAlarmDelivery,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
    ModelLabAlarmDeliveryResult,
)
from omnimarket.nodes.node_canonical_clone_dirty_converge_effect.handlers import (
    handler_canonical_clone_dirty_converge_effect as handler_module,
)
from omnimarket.nodes.node_canonical_clone_dirty_converge_effect.handlers.handler_canonical_clone_dirty_converge_effect import (
    _BREACH,
    AGE_ENV,
    ALARM_NOT_CONFIGURED,
    DETACHED_AGE_ENV,
    ENGINE_BOUND_ENV,
    HandlerCanonicalCloneDirtyConvergeEffect,
    alarm_unrecorded,
    declared_age_seconds,
    declared_detached_age_seconds,
    dirty_converge_command,
    load_declaration,
)
from omnimarket.nodes.node_canonical_clone_dirty_converge_effect.models.model_canonical_clone_dirty_converge import (
    EnumDirtyConvergeStatus,
    EnumLocalWorkKind,
    ModelDirtyConvergeOutcome,
)
from omnimarket.nodes.node_lab_alarm.protocols import ProtocolAlarmDelivery
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay

pytestmark = pytest.mark.unit

REAL_ROOT = os.environ.get("ONEX_TEST_OMNICLAUDE_ROOT", "")
HOST_VALUE = "/" + "Users" + "/someone/Code"  # built, so no home path sits in this file
AGE = 600
#: The contract's bound for every repair (OMN-23032): one host timer period (180 s) is past it.
CONTRACT_BOUND = 150
TIMER_PERIOD = 180


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


def tree_state(clone: Path) -> dict[str, bytes]:
    """Everything a reset or a clean could change in a clone, byte for byte.

    Every file under the working tree (the .git directory's own bookkeeping aside), HEAD, the branch
    heads, the index as a binary diff, and the porcelain status with untracked files.
    """
    state: dict[str, bytes] = {
        path.relative_to(clone).as_posix(): path.read_bytes()
        for path in sorted(clone.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(clone).parts
    }
    for name, args in {
        "@head": ("rev-parse", "HEAD"),
        "@branches": ("for-each-ref", "refs/heads"),
        "@config-bare": ("config", "--get", "core.bare"),
        "@staged": ("diff", "--cached", "--binary"),
        "@status": ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
    }.items():
        done = subprocess.run(
            ["git", "--git-dir", str(clone / ".git"), *args],
            capture_output=True,
            check=False,
            env=scrub_git_location_env(os.environ),
        )
        state[name] = done.stdout
    return state


FAKE_CONVERGE = """#!/bin/bash
clone="$1"; shift
note=""
while [ $# -gt 0 ]; do case "$1" in --note) note="$2"; shift ;; esac; shift; done
git -C "$clone" reset --hard --quiet '@{u}'
echo "2026-10-31T00:00:00Z | STATUS | lane=l | converged $clone; saved: $note." >> "$ROW_FILE"
"""


class Planted:
    """A registry holding one canonical clone that is behind its upstream and dirty."""

    def __init__(self, tmp: Path, name: str, files: dict[str, str]) -> None:
        self.home = tmp / "registry"
        self.home.mkdir()
        self.rows = tmp / "rows.txt"
        seed = tmp / "seed"
        seed.mkdir()
        _git("init", "--quiet", "-b", "dev", cwd=seed)
        _git("config", "user.email", "t@example.com", cwd=seed)
        _git("config", "user.name", "T", cwd=seed)
        for path, text in files.items():
            (seed / path).parent.mkdir(parents=True, exist_ok=True)
            (seed / path).write_text(text)
        _git("add", ".", cwd=seed)
        _git("commit", "--quiet", "-m", "one", cwd=seed)
        self.remote = tmp / "remotes" / "github.com" / "Acme" / f"{name}.git"
        self.remote.parent.mkdir(parents=True)
        _git("clone", "--quiet", "--bare", str(seed), str(self.remote), cwd=tmp)
        _git("remote", "add", "origin", str(self.remote), cwd=seed)
        self.seed = seed
        self.clone = self.home / name
        _git(
            "clone", "--quiet", "-b", "dev", str(self.remote), str(self.clone), cwd=tmp
        )
        _git("config", "user.email", "t@example.com", cwd=self.clone)
        _git("config", "user.name", "T", cwd=self.clone)
        (seed / "newer.txt").write_text("newer\n")
        _git("add", "newer.txt", cwd=seed)
        _git("commit", "--quiet", "-m", "two", cwd=seed)
        _git("push", "--quiet", "origin", "HEAD:dev", cwd=seed)
        self.tip = _git("rev-parse", "HEAD", cwd=seed)
        self.state = tmp / "state"
        if REAL_ROOT:
            self.engine_argv = None
            self._link_real(tmp)
        else:
            self._fake(tmp)

    def _link_real(self, tmp: Path) -> None:
        """Copy the real engine and script beside the planted clone.

        A copy, never a link to the real checkout: the engine syncs every git clone under the registry
        root, and the checkout itself would be one of them. The copy holds no .git, so it is not.
        """
        root = Path(REAL_ROOT)
        for relative in ("plugins/onex/hooks/lib", "scripts"):
            shutil.copytree(
                root / relative,
                self.home / "omniclaude" / relative,
                ignore=shutil.ignore_patterns("__pycache__", "tests"),
            )

    def _fake(self, tmp: Path) -> None:
        script = self.home / "omniclaude/scripts/converge-canonical-clone.sh"
        script.parent.mkdir(parents=True)
        script.write_text(FAKE_CONVERGE)
        self.engine_argv = [sys.executable, "-c", "pass"]

    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            OMNI_HOME=str(self.home),
            ONEX_STATE_DIR=str(self.state),
            ONEX_REGISTRY_ROOTS=str(self.home),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@example.com",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@example.com",
            ROW_FILE=str(self.rows),
        )
        env.pop("ONEX_LEDGER_PATH", None)
        env.update(extra)
        return env

    def handler(self, **extra: str) -> HandlerCanonicalCloneDirtyConvergeEffect:
        return HandlerCanonicalCloneDirtyConvergeEffect(
            self.home, env=self.env(**extra), engine_argv=self.engine_argv
        )

    def refuse_for(
        self, handler: HandlerCanonicalCloneDirtyConvergeEffect, seconds: float
    ) -> None:
        """Real engine: back-date the refusal record. Fake engine: print the breach line."""
        if REAL_ROOT:
            sys.path.insert(0, str(Path(REAL_ROOT) / "plugins/onex/hooks/lib"))
            ccs = importlib.import_module("canonical_clone_sync")

            path = ccs.refusal_record_path(self.env(), self.clone)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "clone": str(self.clone),
                        "first_refused_at": time.time() - seconds,
                    }
                )
            )
            return
        line = f"REFUSED_TOO_LONG {self.clone} refused for {int(seconds)}s: tracked changes"
        fetch = f"git -C '{self.clone}' fetch --quiet origin"
        report = f"echo '{line}'" if seconds >= AGE else "true"
        handler._engine_argv = ["/bin/sh", "-c", f"{fetch}; {report}"]

    def branches(self) -> list[str]:
        return _git(
            "for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=self.remote
        ).splitlines()

    def row_text(self) -> str:
        if self.rows.exists():
            return self.rows.read_text()
        pending = (
            self.home / ".onex_state/canonical-clone-converge/pending-ledger-rows.md"
        )
        return pending.read_text() if pending.exists() else ""


@pytest.fixture
def staged_case(tmp_path: Path) -> Planted:
    """The first case (a host's omniclaude): staged source files, behind upstream."""
    planted = Planted(tmp_path, "platform", {"a.txt": "one\n", "b.txt": "one\n"})
    (planted.clone / "a.txt").write_text("staged source edit\n")
    (planted.clone / "b.txt").write_text("another staged edit\n")
    _git("add", "a.txt", "b.txt", cwd=planted.clone)
    return planted


@pytest.fixture
def generated_case(tmp_path: Path) -> Planted:
    """The second case: a stale generated file that also carries a host value."""
    planted = Planted(
        tmp_path,
        "omnidash",
        {
            "src/data-source-defaults.ts": "// This file is auto-generated. Do not edit.\nexport const a = 1;\n",
            "src/app.ts": "export const app = 1;\n",
        },
    )
    (planted.clone / "src/data-source-defaults.ts").write_text(
        f"// This file is auto-generated. Do not edit.\nexport const a = 2; // {HOST_VALUE}\n"
    )
    return planted


def _clean(clone: Path) -> bool:
    return _git("status", "--porcelain", "--untracked-files=no", cwd=clone) == ""


def test_the_age_is_the_contracts_and_the_overlay_replaces_it() -> None:
    assert declared_age_seconds({}) == CONTRACT_BOUND
    assert declared_age_seconds({AGE_ENV: "90"}) == 90
    assert declared_age_seconds({AGE_ENV: "junk"}) == CONTRACT_BOUND
    assert load_declaration().max_listed >= 1


def test_a_clone_not_yet_refused_for_the_age_is_left_alone(
    staged_case: Planted,
) -> None:
    handler = staged_case.handler(**{AGE_ENV: str(AGE)})
    staged_case.refuse_for(handler, 60)
    before = _git("rev-parse", "HEAD", cwd=staged_case.clone)
    assert handler.tick() == []
    assert _git("rev-parse", "HEAD", cwd=staged_case.clone) == before
    assert (
        _git("diff", "--cached", "--name-only", cwd=staged_case.clone) == "a.txt\nb.txt"
    )


def test_staged_source_files_are_refused_and_kept_byte_for_byte(
    staged_case: Planted,
) -> None:
    """The first real case, now refused: failing control of the reset it used to take."""
    before = tree_state(staged_case.clone)
    handler = staged_case.handler(**{AGE_ENV: str(AGE)})
    staged_case.refuse_for(handler, 3 * AGE)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.REFUSED, outcome
    assert [(w.kind, w.count, w.names) for w in outcome.local_work] == [
        (EnumLocalWorkKind.STAGED, 2, ("a.txt", "b.txt"))
    ]
    assert str(staged_case.clone) in outcome.reason
    assert "staged 2 (a.txt, b.txt)" in outcome.reason
    assert outcome.converged_to == ""
    assert tree_state(staged_case.clone) == before
    assert not staged_case.rows.exists()  # the converge script never ran
    assert staged_case.branches() == ["dev"]


def test_a_stale_generated_file_is_refused_and_its_content_is_kept(
    generated_case: Planted,
) -> None:
    """The second real case (a host value in a generated file): refused, kept, never named in the row."""
    before = tree_state(generated_case.clone)
    handler = generated_case.handler(**{AGE_ENV: str(AGE)})
    generated_case.refuse_for(handler, 3 * AGE)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.REFUSED, outcome
    assert [(w.kind, w.names) for w in outcome.local_work] == [
        (EnumLocalWorkKind.MODIFIED, ("src/data-source-defaults.ts",))
    ]
    assert tree_state(generated_case.clone) == before
    assert (
        HOST_VALUE in (generated_case.clone / "src/data-source-defaults.ts").read_text()
    )
    assert HOST_VALUE not in outcome.reason
    assert not generated_case.rows.exists()


def test_local_commits_on_a_dirty_clone_are_refused_and_named(
    staged_case: Planted,
) -> None:
    clone = staged_case.clone
    (clone / "local.txt").write_text("local commit\n")
    _git("add", "local.txt", cwd=clone)
    _git("commit", "--quiet", "-m", "local", cwd=clone)
    local = _git("rev-parse", "--short", "HEAD", cwd=clone)
    (clone / "a.txt").write_text("dirty again\n")
    before = tree_state(clone)
    handler = staged_case.handler(**{AGE_ENV: str(AGE)})
    staged_case.refuse_for(handler, 3 * AGE)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.REFUSED, outcome
    kinds = {w.kind: w for w in outcome.local_work}
    # the commit took the two staged files with it, so only the new edit and the commit remain
    assert set(kinds) == {
        EnumLocalWorkKind.MODIFIED,
        EnumLocalWorkKind.UNPUSHED_COMMITS,
    }
    assert kinds[EnumLocalWorkKind.UNPUSHED_COMMITS].names == (local,)
    assert f"unpushed_commits 1 ({local})" in outcome.reason
    assert tree_state(clone) == before


def test_a_clone_an_open_claim_names_is_left_alone(
    staged_case: Planted, tmp_path: Path
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "2026-10-31T10:00:00Z | CLAIM | lane=other-lane | ticket=OMN-1 | works in "
        f"{staged_case.clone} right now\n"
    )
    handler = staged_case.handler(
        **{AGE_ENV: str(AGE), "ONEX_LEDGER_PATH": str(ledger)}
    )
    staged_case.refuse_for(handler, 3 * AGE)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.SKIPPED
    assert "other-lane" in outcome.reason
    assert (
        _git("diff", "--cached", "--name-only", cwd=staged_case.clone) == "a.txt\nb.txt"
    )
    ledger.write_text(
        ledger.read_text()
        + "2026-10-31T11:00:00Z | TERMINAL | lane=other-lane | ticket=OMN-1 | done\n"
    )
    assert (
        staged_case.handler(
            **{AGE_ENV: str(AGE), "ONEX_LEDGER_PATH": str(ledger)}
        )._open_claim_naming(staged_case.clone)
        == ""
    )


def test_a_clone_a_process_works_inside_is_left_alone(staged_case: Planted) -> None:
    handler = staged_case.handler(**{AGE_ENV: str(AGE)})
    staged_case.refuse_for(handler, 3 * AGE)
    with subprocess.Popen(["sleep", "60"], cwd=staged_case.clone) as sleeper:
        try:
            (outcome,) = handler.tick()
        finally:
            sleeper.kill()
    assert outcome.status is EnumDirtyConvergeStatus.SKIPPED
    assert "process" in outcome.reason
    assert (
        _git("diff", "--cached", "--name-only", cwd=staged_case.clone) == "a.txt\nb.txt"
    )


# -- a detached HEAD (OMN-20344) -------------------------------------------------------------

DETACHED_AGE = 300
#: The fake converge script of a detached clone: re-attach to dev, then fast-forward, as the real one does.
FAKE_REATTACH = """#!/bin/bash
clone="$1"; shift
note=""
while [ $# -gt 0 ]; do case "$1" in --note) note="$2"; shift ;; esac; shift; done
git -C "$clone" switch --quiet dev
git -C "$clone" reset --hard --quiet '@{u}'
echo "2026-11-03T00:00:00Z | STATUS | lane=l | converged $clone; saved: $note." >> "$ROW_FILE"
"""
FAKE_UNABLE = """#!/bin/bash
echo "refusing: no re-attachment target can be derived" >&2
exit 2
"""


@pytest.fixture
def detached_case(tmp_path: Path) -> Planted:
    """A clean canonical clone left on a detached HEAD at an old commit, behind its upstream."""
    planted = Planted(tmp_path, "omnimarket", {"a.txt": "one\n"})
    _git("checkout", "--quiet", "--detach", "HEAD", cwd=planted.clone)
    if not REAL_ROOT:
        (planted.home / "omniclaude/scripts/converge-canonical-clone.sh").write_text(
            FAKE_REATTACH
        )
    return planted


def _report_detached(
    handler: HandlerCanonicalCloneDirtyConvergeEffect, planted: Planted, seconds: int
) -> None:
    """The engine's breach line for the detached clone, after a fetch (the real engine's own work)."""
    if REAL_ROOT:
        planted.refuse_for(handler, seconds)
        return
    fetch = f"git -C '{planted.clone}' fetch --quiet origin"
    line = f"REFUSED_TOO_LONG {planted.clone} refused for {seconds}s: detached HEAD"
    handler._engine_argv = ["/bin/sh", "-c", f"{fetch}; echo '{line}'"]


def test_the_detached_age_is_the_contracts_and_the_overlay_replaces_it() -> None:
    assert declared_detached_age_seconds({}) == CONTRACT_BOUND
    assert declared_detached_age_seconds({DETACHED_AGE_ENV: "90"}) == 90
    assert declared_detached_age_seconds({DETACHED_AGE_ENV: "junk"}) == CONTRACT_BOUND
    assert declared_detached_age_seconds({}) < 3600  # inside the hour the ticket allows


def test_lab_clone_detached_reconciled_within_bound(detached_case: Planted) -> None:
    clone = detached_case.clone
    assert _git("branch", "--show-current", cwd=clone) == ""  # planted detached
    handler = detached_case.handler(
        **{AGE_ENV: str(AGE), DETACHED_AGE_ENV: str(DETACHED_AGE)}
    )
    _report_detached(handler, detached_case, 2 * DETACHED_AGE)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.CONVERGED, outcome
    assert _git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=clone) == "dev"
    assert _git("rev-parse", "HEAD", cwd=clone) == detached_case.tip
    assert outcome.converged_to == detached_case.tip
    assert detached_case.branches() == ["dev"]
    assert "reattached from a detached HEAD" in detached_case.row_text()


def test_lab_clone_detached_reconcile_bound_is_shorter_than_the_dirty_age(
    detached_case: Planted,
) -> None:
    """The engine is handed the smaller bound, so a detached clone is reported before a dirty one would be."""
    handler = detached_case.handler(
        **{AGE_ENV: str(AGE), DETACHED_AGE_ENV: str(DETACHED_AGE)}
    )
    assert min(handler.age, handler.detached_age) == DETACHED_AGE
    _report_detached(handler, detached_case, DETACHED_AGE + 1)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.CONVERGED, outcome


def test_lab_clone_detached_reconcile_that_cannot_fails_loud(
    detached_case: Planted,
) -> None:
    if REAL_ROOT:
        pytest.skip("drives the fake converge script that refuses")
    script = detached_case.home / "omniclaude/scripts/converge-canonical-clone.sh"
    script.write_text(FAKE_UNABLE)
    handler = detached_case.handler(
        **{AGE_ENV: str(AGE), DETACHED_AGE_ENV: str(DETACHED_AGE)}
    )
    _report_detached(handler, detached_case, 2 * DETACHED_AGE)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.FAILED
    assert "still detached" in outcome.reason
    assert "exit 2" in outcome.reason
    assert _git("branch", "--show-current", cwd=detached_case.clone) == ""


def test_a_detached_clone_an_open_claim_names_is_left_alone(
    detached_case: Planted, tmp_path: Path
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "2026-11-03T10:00:00Z | CLAIM | lane=other-lane | ticket=OMN-1 | works in "
        f"{detached_case.clone} right now\n"
    )
    handler = detached_case.handler(
        **{
            AGE_ENV: str(AGE),
            DETACHED_AGE_ENV: str(DETACHED_AGE),
            "ONEX_LEDGER_PATH": str(ledger),
        }
    )
    _report_detached(handler, detached_case, 2 * DETACHED_AGE)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.SKIPPED
    assert "other-lane" in outcome.reason
    assert _git("branch", "--show-current", cwd=detached_case.clone) == ""


@pytest.mark.parametrize(
    ("refused_for", "expected_status"),
    [
        (DETACHED_AGE - 1, EnumDirtyConvergeStatus.SKIPPED),
        (DETACHED_AGE, EnumDirtyConvergeStatus.FAILED),
        (2 * DETACHED_AGE, EnumDirtyConvergeStatus.FAILED),
    ],
)
def test_lab_clone_detached_reconcile_missing_head_commit(
    detached_case: Planted,
    refused_for: int,
    expected_status: EnumDirtyConvergeStatus,
) -> None:
    clone = detached_case.clone
    (clone / "local.txt").write_text("local commit\n")
    _git("add", "local.txt", cwd=clone)
    _git("commit", "--quiet", "-m", "local commit", cwd=clone)
    head = _git("rev-parse", "HEAD", cwd=clone)
    (clone / ".git/objects" / head[:2] / head[2:]).unlink()
    missing = subprocess.run(
        ["git", "cat-file", "-e", f"{head}^{{commit}}"],
        cwd=clone,
        capture_output=True,
        text=True,
        check=False,
        env=scrub_git_location_env(os.environ),
    )
    assert missing.returncode != 0
    handler = detached_case.handler(
        **{AGE_ENV: str(AGE), DETACHED_AGE_ENV: str(DETACHED_AGE)}
    )
    _report_detached(handler, detached_case, refused_for)

    (outcome,) = handler.tick()

    assert outcome.status is expected_status, outcome
    assert outcome.clone == str(clone)
    assert outcome.refused_for_seconds == refused_for
    if expected_status is EnumDirtyConvergeStatus.FAILED:
        assert "HEAD does not resolve" in outcome.reason
    else:
        assert "below the declared" in outcome.reason
    assert (clone / ".git/HEAD").read_text().strip() == head
    assert (clone / "local.txt").read_text() == "local commit\n"


def test_a_dirty_clone_refused_only_for_the_detached_bound_is_left_alone(
    staged_case: Planted,
) -> None:
    """Positive control: the shorter detached bound does not shorten the dirty clone's age."""
    handler = staged_case.handler(
        **{AGE_ENV: str(AGE), DETACHED_AGE_ENV: str(DETACHED_AGE)}
    )
    line = f"REFUSED_TOO_LONG {staged_case.clone} refused for {DETACHED_AGE + 1}s: tracked changes"
    handler._engine_argv = ["/bin/sh", "-c", f"echo '{line}'"]
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.SKIPPED
    assert "below the declared" in outcome.reason
    assert (
        _git("diff", "--cached", "--name-only", cwd=staged_case.clone) == "a.txt\nb.txt"
    )


# -- a mirror repairs itself on the next run (OMN-23032) -------------------------------------


def _next_run(
    handler: HandlerCanonicalCloneDirtyConvergeEffect, planted: Planted, why: str
) -> None:
    """The engine's report one timer period after it first refused the clone."""
    if REAL_ROOT:
        planted.refuse_for(handler, TIMER_PERIOD)
        return
    fetch = f"git -C '{planted.clone}' fetch --quiet origin"
    line = f"REFUSED_TOO_LONG {planted.clone} refused for {TIMER_PERIOD}s: {why}"
    handler._engine_argv = ["/bin/sh", "-c", f"{fetch}; echo '{line}'"]


def test_a_planted_detached_clone_is_reattached_on_the_next_run(
    detached_case: Planted,
) -> None:
    """AC1 falsifier: no overlay value, the contract's own bound, one timer period later."""
    clone = detached_case.clone
    assert _git("branch", "--show-current", cwd=clone) == ""  # planted detached
    handler = detached_case.handler()
    assert handler.detached_age <= TIMER_PERIOD
    _next_run(handler, detached_case, "detached HEAD: the clone tracks no branch")
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.CONVERGED, outcome
    assert _git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=clone) == "dev"
    assert _git("rev-parse", "HEAD", cwd=clone) == detached_case.tip


def test_a_clone_parked_on_a_lane_branch_returns_to_its_branch_on_the_next_run(
    detached_case: Planted,
) -> None:
    clone = detached_case.clone
    _git("switch", "--quiet", "-c", "omn-1-lane-work", cwd=clone)
    handler = detached_case.handler()
    _next_run(handler, detached_case, "checked out on 'omn-1-lane-work'")
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.CONVERGED, outcome
    assert "the lane branch omn-1-lane-work" in outcome.reason
    assert _git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=clone) == "dev"
    assert _git("rev-parse", "HEAD", cwd=clone) == detached_case.tip


def test_a_clean_clone_holding_a_local_commit_is_refused_on_the_next_run(
    staged_case: Planted,
) -> None:
    clone = staged_case.clone
    _git(
        "reset", "--quiet", "--hard", "HEAD", cwd=clone
    )  # clean: only the commit holds it back
    (clone / "local.txt").write_text("local commit\n")
    _git("add", "local.txt", cwd=clone)
    _git("commit", "--quiet", "-m", "local", cwd=clone)
    local = _git("rev-parse", "--short", "HEAD", cwd=clone)
    before = tree_state(clone)
    handler = staged_case.handler()
    _next_run(handler, staged_case, "dev and origin/dev have diverged")
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.REFUSED, outcome
    assert [(w.kind, w.names) for w in outcome.local_work] == [
        (EnumLocalWorkKind.UNPUSHED_COMMITS, (local,))
    ]
    assert _git("rev-parse", "--short", "HEAD", cwd=clone) == local
    assert tree_state(clone) == before


def test_a_dirty_clone_is_refused_on_the_next_run_under_the_contract_bound(
    staged_case: Planted,
) -> None:
    before = tree_state(staged_case.clone)
    handler = staged_case.handler()
    _next_run(handler, staged_case, "2 staged path(s)")
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.REFUSED, outcome
    assert tree_state(staged_case.clone) == before


# -- an image build holds the reconcile-host lock (OMN-22718) -----------------------------------------
#
# The deploy agent stages its siblings into the canonical omnibase_infra clone, then builds. A tracked
# placeholder it staged reads as a dirty clone, and an off-lineage build (an open pull request's head)
# reads as a detached clone. Past the contract bound the tick converged the clone back to dev and the
# agent's second build copied the placeholder: "Per-repo VCS provenance has no sibling entries"
# (read twice on one host, a hard reset of the clone 3 to 4 minutes after each build began).

BUILD_LOCK = ".onex-reconcile-host.lock"


def _hold_build_lock(
    planted: Planted,
    *,
    parent: bool,
    age: int = 0,
    pid: int = 1,
    host: str = "h",
) -> Path:
    lock = (planted.home.parent if parent else planted.home) / BUILD_LOCK
    lock.mkdir()
    holder = lock / "holder"
    holder.write_text(
        f"pid={pid}\nhost={host}\nholder=deploy-agent\n"
        "purpose=deploy-agent image build 550770d47df4\n"
    )
    if age:
        past = time.time() - age
        os.utime(holder, (past, past))
        os.utime(lock, (past, past))
    return lock


@pytest.mark.parametrize("parent", [False, True], ids=["registry-lock", "host-lock"])
def test_a_dirty_clone_is_left_alone_while_an_image_build_holds_the_lock(
    staged_case: Planted, parent: bool
) -> None:
    _hold_build_lock(staged_case, parent=parent)
    handler = staged_case.handler(**{AGE_ENV: str(AGE)})
    staged_case.refuse_for(handler, 3 * AGE)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.SKIPPED
    assert BUILD_LOCK in outcome.reason
    assert (
        _git("diff", "--cached", "--name-only", cwd=staged_case.clone) == "a.txt\nb.txt"
    )


@pytest.mark.parametrize("parent", [False, True], ids=["registry-lock", "host-lock"])
def test_a_detached_clone_is_left_alone_while_an_image_build_holds_the_lock(
    detached_case: Planted, parent: bool
) -> None:
    _hold_build_lock(detached_case, parent=parent)
    handler = detached_case.handler(
        **{AGE_ENV: str(AGE), DETACHED_AGE_ENV: str(DETACHED_AGE)}
    )
    _report_detached(handler, detached_case, 2 * DETACHED_AGE)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.SKIPPED
    assert BUILD_LOCK in outcome.reason
    assert _git("branch", "--show-current", cwd=detached_case.clone) == ""


def test_a_build_lock_past_its_declared_age_does_not_hold_the_clone(
    staged_case: Planted,
) -> None:
    max_age = load_declaration().build_lock_max_age_seconds
    _hold_build_lock(staged_case, parent=False, age=max_age + 60)
    handler = staged_case.handler(**{AGE_ENV: str(AGE)})
    staged_case.refuse_for(handler, 3 * AGE)
    before = tree_state(staged_case.clone)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.REFUSED, (
        outcome
    )  # not held, and never reset
    assert tree_state(staged_case.clone) == before


def test_the_build_lock_age_outlasts_the_deploy_agent_build_ceiling() -> None:
    """The agent's image build is bounded at 3600 s; a lock younger than that is a live build."""
    assert load_declaration().build_lock_max_age_seconds > 3600


def _exited_pid() -> int:
    """The pid of a child that has exited and been reaped: no process holds it now."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_a_build_lock_held_by_a_running_process_on_this_host_holds_the_clone(
    staged_case: Planted,
) -> None:
    """The recorded job: the agent's holder record names this host and its own live pid."""
    _hold_build_lock(
        staged_case, parent=False, pid=os.getpid(), host=socket.gethostname()
    )
    handler = staged_case.handler(**{AGE_ENV: str(AGE)})
    staged_case.refuse_for(handler, 3 * AGE)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.SKIPPED
    assert BUILD_LOCK in outcome.reason


def test_a_build_lock_whose_holder_process_is_gone_does_not_hold_the_clone(
    staged_case: Planted,
) -> None:
    """A crashed agent leaves its lock behind; the clone is read without waiting out the age."""
    _hold_build_lock(
        staged_case, parent=False, pid=_exited_pid(), host=socket.gethostname()
    )
    handler = staged_case.handler(**{AGE_ENV: str(AGE)})
    staged_case.refuse_for(handler, 3 * AGE)
    before = tree_state(staged_case.clone)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.REFUSED, (
        outcome
    )  # not held, and never reset
    assert tree_state(staged_case.clone) == before


# -- a clone git wrongly treats as bare (OMN-24240) --------------------------------------------


def _report_bare(
    handler: HandlerCanonicalCloneDirtyConvergeEffect, planted: Planted, seconds: int
) -> None:
    """The engine's breach line for the bare-flagged clone, after a fetch (the real engine's own work)."""
    if REAL_ROOT:
        planted.refuse_for(handler, seconds)
        return
    fetch = f"git -C '{planted.clone}' fetch --quiet origin"
    line = f"REFUSED_TOO_LONG {planted.clone} refused for {seconds}s: core.bare=true"
    handler._engine_argv = ["/bin/sh", "-c", f"{fetch}; echo '{line}'"]


@pytest.fixture
def bare_flagged_case(tmp_path: Path) -> Planted:
    """A canonical clone with a working tree that git wrongly treats as bare (core.bare=true)."""
    planted = Planted(tmp_path, "platform", {"a.txt": "one\n"})
    _git("config", "core.bare", "true", cwd=planted.clone)
    if not REAL_ROOT:
        (planted.home / "omniclaude/scripts/converge-canonical-clone.sh").write_text(
            FAKE_REATTACH
        )
    return planted


def test_a_clone_flagged_bare_with_a_working_tree_is_repaired_and_brought_to_the_tip(
    bare_flagged_case: Planted,
) -> None:
    """Golden: the handler clears core.bare and converges the clone to the upstream tip."""
    clone = bare_flagged_case.clone
    assert _git("rev-parse", "--is-bare-repository", cwd=clone) == "true"
    handler = bare_flagged_case.handler(
        **{AGE_ENV: str(AGE), DETACHED_AGE_ENV: str(DETACHED_AGE)}
    )
    _report_bare(handler, bare_flagged_case, 600)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.CONVERGED, outcome
    assert "cleared core.bare=true" in outcome.reason
    assert _git("rev-parse", "--is-bare-repository", cwd=clone) == "false"
    assert _git("rev-parse", "HEAD", cwd=clone) == bare_flagged_case.tip
    assert outcome.converged_to == bare_flagged_case.tip
    assert bare_flagged_case.row_text()


def test_a_true_bare_mirror_is_left_alone(bare_flagged_case: Planted) -> None:
    """A true bare mirror has no .git directory inside it; the handler never touches one."""
    planted = bare_flagged_case
    shutil.rmtree(planted.clone)
    _git("clone", "--bare", str(planted.remote), str(planted.clone), cwd=planted.home)
    handler = planted.handler(
        **{AGE_ENV: str(AGE), DETACHED_AGE_ENV: str(DETACHED_AGE)}
    )
    outcome = handler.converge_clone(planted.clone, refused_for=2 * DETACHED_AGE)
    assert outcome.status is EnumDirtyConvergeStatus.SKIPPED
    assert "no working tree" in outcome.reason
    assert _git("config", "--get", "core.bare", cwd=planted.clone) == "true"


def test_a_bare_flagged_clone_below_the_age_is_left_alone(
    bare_flagged_case: Planted,
) -> None:
    handler = bare_flagged_case.handler(
        **{AGE_ENV: str(AGE), DETACHED_AGE_ENV: str(DETACHED_AGE)}
    )
    outcome = handler.converge_clone(bare_flagged_case.clone, refused_for=1)
    assert outcome.status is EnumDirtyConvergeStatus.SKIPPED
    assert "below the declared" in outcome.reason
    assert _git("config", "--get", "core.bare", cwd=bare_flagged_case.clone) == "true"


def test_a_bare_flagged_clone_an_open_claim_names_is_left_alone(
    bare_flagged_case: Planted, tmp_path: Path
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "2026-11-03T10:00:00Z | CLAIM | lane=other-lane | ticket=OMN-1 | works in "
        f"{bare_flagged_case.clone} right now\n"
    )
    handler = bare_flagged_case.handler(
        **{
            AGE_ENV: str(AGE),
            DETACHED_AGE_ENV: str(DETACHED_AGE),
            "ONEX_LEDGER_PATH": str(ledger),
        }
    )
    _report_bare(handler, bare_flagged_case, 2 * DETACHED_AGE)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.SKIPPED
    assert "other-lane" in outcome.reason
    assert _git("config", "--get", "core.bare", cwd=bare_flagged_case.clone) == "true"


def test_a_bare_flagged_clone_a_process_works_inside_is_left_alone(
    bare_flagged_case: Planted,
) -> None:
    handler = bare_flagged_case.handler(
        **{AGE_ENV: str(AGE), DETACHED_AGE_ENV: str(DETACHED_AGE)}
    )
    _report_bare(handler, bare_flagged_case, 2 * DETACHED_AGE)
    with subprocess.Popen(["sleep", "60"], cwd=bare_flagged_case.clone) as sleeper:
        try:
            (outcome,) = handler.tick()
        finally:
            sleeper.kill()
    assert outcome.status is EnumDirtyConvergeStatus.SKIPPED
    assert "process" in outcome.reason
    assert _git("config", "--get", "core.bare", cwd=bare_flagged_case.clone) == "true"


def test_a_bare_flagged_clone_whose_converge_script_fails_reports_failed(
    bare_flagged_case: Planted,
) -> None:
    if REAL_ROOT:
        pytest.skip("drives the fake converge script that refuses")
    script = bare_flagged_case.home / "omniclaude/scripts/converge-canonical-clone.sh"
    script.write_text("#!/bin/bash\necho boom >&2\nexit 1\n")
    handler = bare_flagged_case.handler(
        **{AGE_ENV: str(AGE), DETACHED_AGE_ENV: str(DETACHED_AGE)}
    )
    _report_bare(handler, bare_flagged_case, 2 * DETACHED_AGE)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.FAILED
    assert "exit 1" in outcome.reason


def test_a_bare_flagged_clone_is_left_alone_while_an_image_build_holds_the_lock(
    bare_flagged_case: Planted,
) -> None:
    _hold_build_lock(bare_flagged_case, parent=False)
    handler = bare_flagged_case.handler(
        **{AGE_ENV: str(AGE), DETACHED_AGE_ENV: str(DETACHED_AGE)}
    )
    _report_bare(handler, bare_flagged_case, 2 * DETACHED_AGE)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.SKIPPED
    assert BUILD_LOCK in outcome.reason
    assert _git("config", "--get", "core.bare", cwd=bare_flagged_case.clone) == "true"


# -- a clone with local work is never converged (OMN-19998) ------------------------------------
#
# The failing controls of the safety defect: the Mac's registry-root clone held one unpushed commit and
# staged or modified tracked files, and its knowledge base clone held 280 uncommitted added lines in a
# tracked file plus untracked files; the declared tick would have reset both. Each kind of local work
# is planted alone on an otherwise clean clone, and each must be REFUSED with the clone and the kind
# named, leave every byte as it was, and run no converge script.


def _plant_unpushed_commit(clone: Path) -> str:
    (clone / "local.txt").write_text("local commit\n")
    _git("add", "local.txt", cwd=clone)
    _git("commit", "--quiet", "-m", "local", cwd=clone)
    return _git("rev-parse", "--short", "HEAD", cwd=clone)


def _plant_staged(clone: Path) -> str:
    (clone / "a.txt").write_text("staged edit\n")
    _git("add", "a.txt", cwd=clone)
    return "a.txt"


def _plant_modified(clone: Path) -> str:
    (clone / "a.txt").write_text("worktree edit, not staged\n")
    return "a.txt"


def _plant_untracked(clone: Path) -> str:
    (clone / "notes").mkdir()
    (clone / "notes/kept.md").write_text("an untracked note\n")
    return "notes/kept.md"


KIND_PLANTS = {
    EnumLocalWorkKind.UNPUSHED_COMMITS: _plant_unpushed_commit,
    EnumLocalWorkKind.STAGED: _plant_staged,
    EnumLocalWorkKind.MODIFIED: _plant_modified,
    EnumLocalWorkKind.UNTRACKED: _plant_untracked,
}


@pytest.fixture
def clean_case(tmp_path: Path) -> Planted:
    """A clean clone, behind its upstream, that holds nothing of its own."""
    return Planted(tmp_path, "platform", {"a.txt": "one\n", "b.txt": "one\n"})


def _decide(
    planted: Planted,
    delivery: ProtocolAlarmDelivery | None = None,
    *,
    refused_for: int = 3 * AGE,
) -> list[ModelDirtyConvergeOutcome]:
    """What the tick does with one clone the engine named, without the engine in the way.

    The real engine names a clone only for tracked changes, a commit it does not have, a detached HEAD
    and the like, never for an untracked file alone, so a case that must reach the handler's own
    decision goes through converge_clone and the alarm step directly. The cases that cross the
    engine's line use tick() and run against the real engine too (ONEX_TEST_OMNICLAUDE_ROOT).
    """
    _git("fetch", "--quiet", "origin", cwd=planted.clone)  # the engine's own first step
    handler = HandlerCanonicalCloneDirtyConvergeEffect(
        planted.home,
        env=planted.env(**{AGE_ENV: str(AGE)}),
        engine_argv=planted.engine_argv,
        delivery=delivery,
    )
    return handler.raise_alarms([handler.converge_clone(planted.clone, refused_for)])


@pytest.mark.parametrize("kind", list(KIND_PLANTS), ids=lambda k: k.value)
def test_a_clone_holding_one_kind_of_local_work_is_refused_and_kept_byte_for_byte(
    clean_case: Planted, kind: EnumLocalWorkKind
) -> None:
    name = KIND_PLANTS[kind](clean_case.clone)
    before = tree_state(clean_case.clone)
    (outcome,) = _decide(clean_case)
    assert outcome.status is EnumDirtyConvergeStatus.REFUSED, outcome
    assert [(w.kind, w.names) for w in outcome.local_work] == [(kind, (name,))]
    assert outcome.reason.startswith(
        f"refused: clone {clean_case.clone} holds local work"
    )
    assert f"{kind.value} 1 ({name})" in outcome.reason
    assert outcome.reason.endswith("left as found")
    assert outcome.converged_to == ""
    assert tree_state(clean_case.clone) == before
    assert not clean_case.rows.exists()  # the converge script never ran


def test_a_clean_clone_with_no_unpushed_commit_is_not_refused_as_holding_work(
    clean_case: Planted,
) -> None:
    """Positive control: the same clone without local work reads as clean, so a refusal is the work's."""
    before = tree_state(clean_case.clone)
    (outcome,) = _decide(clean_case)
    assert outcome.status is EnumDirtyConvergeStatus.SKIPPED, outcome
    assert outcome.local_work == ()
    assert "no local work" in outcome.reason
    assert tree_state(clean_case.clone) == before


def test_every_kind_at_once_is_named_and_a_commit_already_pushed_is_not_local_work(
    clean_case: Planted,
) -> None:
    clone = clean_case.clone
    (clone / "pushed.txt").write_text("pushed\n")
    _git("add", "pushed.txt", cwd=clone)
    _git("commit", "--quiet", "-m", "pushed", cwd=clone)
    _git("pull", "--quiet", "--rebase", "origin", "dev", cwd=clone)
    _git("push", "--quiet", "origin", "HEAD:dev", cwd=clone)
    _git("fetch", "--quiet", "origin", cwd=clone)
    local = _plant_unpushed_commit(clone)
    _plant_staged(clone)
    (clone / "b.txt").write_text("modified\n")
    _plant_untracked(clone)
    before = tree_state(clone)
    work = clean_case.handler()._local_work(clone)
    by_kind = {w.kind: w for w in work}
    assert by_kind[EnumLocalWorkKind.UNPUSHED_COMMITS].names == (local,)
    assert by_kind[EnumLocalWorkKind.STAGED].names == ("a.txt",)
    assert by_kind[EnumLocalWorkKind.MODIFIED].names == ("b.txt",)
    assert by_kind[EnumLocalWorkKind.UNTRACKED].names == ("notes/kept.md",)
    assert tree_state(clone) == before  # reading it changed nothing


def test_many_paths_are_counted_and_only_the_declared_number_is_named(
    clean_case: Planted,
) -> None:
    listed = load_declaration().max_listed
    for index in range(listed + 3):
        (clean_case.clone / f"extra{index:02d}.txt").write_text("x\n")
    (outcome,) = _decide(clean_case)
    (work,) = outcome.local_work
    assert work.count == listed + 3
    assert len(work.names) == listed
    assert "and 3 more" in outcome.reason


def test_a_clone_whose_local_work_cannot_be_read_is_failed_and_never_converged(
    clean_case: Planted,
) -> None:
    """Error chain: an unreadable clone is not taken for a clean one."""
    handler = clean_case.handler(**{AGE_ENV: str(AGE)})
    real = handler._run

    def run(argv: list[str], **kwargs: object) -> object:
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 128, "", "fatal: broken index")
        return real(argv, **kwargs)

    handler._run = run
    _git("fetch", "--quiet", "origin", cwd=clean_case.clone)
    before = tree_state(clean_case.clone)
    outcome = handler.converge_clone(clean_case.clone, 3 * AGE)
    assert outcome.status is EnumDirtyConvergeStatus.FAILED, outcome
    assert "could not be read" in outcome.reason
    assert tree_state(clean_case.clone) == before
    assert not clean_case.rows.exists()


@pytest.mark.parametrize(
    "plant",
    [_plant_staged, _plant_modified, _plant_untracked],
    ids=["staged", "modified", "untracked"],
)
def test_a_detached_clone_holding_local_work_is_refused_not_reattached(
    detached_case: Planted, plant: Callable[[Path], str]
) -> None:
    plant(detached_case.clone)
    before = tree_state(detached_case.clone)
    handler = detached_case.handler()
    _next_run(handler, detached_case, "detached HEAD: the clone tracks no branch")
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.REFUSED, outcome
    assert _git("branch", "--show-current", cwd=detached_case.clone) == ""
    assert tree_state(detached_case.clone) == before
    assert not detached_case.rows.exists()


def test_a_detached_clone_holding_a_commit_no_remote_has_is_refused(
    detached_case: Planted,
) -> None:
    local = _plant_unpushed_commit(detached_case.clone)
    before = tree_state(detached_case.clone)
    handler = detached_case.handler()
    _next_run(handler, detached_case, "detached HEAD: the clone tracks no branch")
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.REFUSED, outcome
    assert outcome.local_work[0].names == (local,)
    assert tree_state(detached_case.clone) == before


def test_a_detached_clone_whose_tracking_branch_holds_an_unpushed_commit_is_refused(
    detached_case: Planted,
) -> None:
    """The reattach resets the tracking branch, so a commit only that branch holds is local work."""
    clone = detached_case.clone
    _git("switch", "--quiet", "dev", cwd=clone)
    local = _plant_unpushed_commit(clone)
    _git("checkout", "--quiet", "--detach", "origin/dev", cwd=clone)
    before = tree_state(clone)
    handler = detached_case.handler()
    _next_run(handler, detached_case, "detached HEAD: the clone tracks no branch")
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.REFUSED, outcome
    assert outcome.local_work[0].names == (local,)
    assert tree_state(clone) == before


def test_a_clone_parked_on_a_lane_branch_with_an_untracked_file_is_refused(
    detached_case: Planted,
) -> None:
    clone = detached_case.clone
    _git("switch", "--quiet", "-c", "omn-1-lane-work", cwd=clone)
    _plant_untracked(clone)
    before = tree_state(clone)
    handler = detached_case.handler()
    _next_run(handler, detached_case, "checked out on 'omn-1-lane-work'")
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.REFUSED, outcome
    assert _git("branch", "--show-current", cwd=clone) == "omn-1-lane-work"
    assert tree_state(clone) == before


@pytest.mark.parametrize(
    "plant",
    [_plant_staged, _plant_modified, _plant_untracked, _plant_unpushed_commit],
    ids=["staged", "modified", "untracked", "unpushed_commits"],
)
def test_a_bare_flagged_clone_holding_local_work_keeps_its_flag_and_its_work(
    bare_flagged_case: Planted, plant: Callable[[Path], str]
) -> None:
    clone = bare_flagged_case.clone
    _git("config", "core.bare", "false", cwd=clone)
    plant(clone)
    _git("config", "core.bare", "true", cwd=clone)
    before = tree_state(clone)
    handler = bare_flagged_case.handler(
        **{AGE_ENV: str(AGE), DETACHED_AGE_ENV: str(DETACHED_AGE)}
    )
    _report_bare(handler, bare_flagged_case, 2 * DETACHED_AGE)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.REFUSED, outcome
    assert _git("config", "--get", "core.bare", cwd=clone) == "true"
    assert tree_state(clone) == before
    assert not bare_flagged_case.rows.exists()


# -- the owned alarm ------------------------------------------------------------------------------


class RecordingDelivery:
    """A delivery that records every request and answers delivered, or raises what it is given."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.requests: list[ModelLabAlarmDeliveryRequest] = []
        self.raises = raises

    def handle(
        self, request: ModelLabAlarmDeliveryRequest
    ) -> ModelLabAlarmDeliveryResult:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        return ModelLabAlarmDeliveryResult(
            correlation_id=request.correlation_id,
            condition=request.condition,
            outcome=EnumDeliveryOutcome.DELIVERED
            if request.alarms
            else EnumDeliveryOutcome.NOTHING_NEW,
            deliveries=tuple(
                ModelAlarmDelivery(
                    key=a.key,
                    channel=EnumDeliveryStep.DELIVERED,
                    ledger=EnumDeliveryStep.DELIVERED,
                )
                for a in request.alarms
            ),
            cleared=(),
            retried=(),
            state_kept=True,
        )


def _refused_tick(
    planted: Planted, delivery: ProtocolAlarmDelivery | None
) -> list[ModelDirtyConvergeOutcome]:
    return _decide(planted, delivery)


def test_a_refused_clone_raises_one_owned_alarm_under_the_contracts_condition(
    clean_case: Planted,
) -> None:
    name = _plant_staged(clean_case.clone)
    delivery = RecordingDelivery()
    (outcome,) = _refused_tick(clean_case, delivery)
    assert outcome.alarm == "delivered"
    (request,) = delivery.requests
    condition = load_declaration().alarm_condition
    assert condition == "clone_local_work_refused"
    assert request.condition == condition
    assert request.outcome is EnumDeliveryCondition.ALARM
    (alarm,) = request.alarms
    assert alarm.subject == str(clean_case.clone)
    assert alarm.detail == outcome.reason
    assert f"staged 1 ({name})" in alarm.detail


def test_a_tick_that_refuses_nothing_hands_the_alarm_an_ok_so_an_episode_can_end(
    clean_case: Planted,
) -> None:
    delivery = RecordingDelivery()
    (outcome,) = _refused_tick(clean_case, delivery)  # clean: skipped, nothing refused
    assert outcome.status is EnumDirtyConvergeStatus.SKIPPED
    assert outcome.alarm == ""
    (request,) = delivery.requests
    assert request.outcome is EnumDeliveryCondition.OK
    assert request.alarms == ()


def test_a_refused_clone_on_a_host_with_no_alarm_delivery_says_so_and_fails_the_tick(
    clean_case: Planted,
) -> None:
    _plant_untracked(clean_case.clone)
    outcomes = _refused_tick(clean_case, None)
    assert [o.alarm for o in outcomes] == [ALARM_NOT_CONFIGURED]
    assert alarm_unrecorded(outcomes)


@pytest.mark.parametrize(
    "error",
    [OSError("ledger bus unreachable"), ValueError("overlay invalid")],
    ids=["oserror", "valueerror"],
)
def test_an_alarm_delivery_that_raises_is_named_on_the_outcome_and_fails_the_tick(
    clean_case: Planted, error: Exception
) -> None:
    """Error chain: the refusal stands, the alarm failure is recorded and never swallowed."""
    _plant_modified(clean_case.clone)
    before = tree_state(clean_case.clone)
    outcomes = _refused_tick(clean_case, RecordingDelivery(raises=error))
    (outcome,) = outcomes
    assert outcome.status is EnumDirtyConvergeStatus.REFUSED
    assert outcome.alarm.startswith("failed: ")
    assert str(error) in outcome.alarm
    assert alarm_unrecorded(outcomes)
    assert tree_state(clean_case.clone) == before


@pytest.mark.parametrize(
    ("alarmed", "code"), [(True, 0), (False, 1)], ids=["alarmed", "not-alarmed"]
)
def test_the_command_exits_one_when_a_refusal_was_not_alarmed_and_zero_when_it_was(
    clean_case: Planted,
    monkeypatch: pytest.MonkeyPatch,
    alarmed: bool,
    code: int,
) -> None:
    _plant_staged(clean_case.clone)
    delivery = RecordingDelivery() if alarmed else None
    monkeypatch.setattr(handler_module, "delivery_from_overlay", lambda _env: delivery)
    built = handler_module.HandlerCanonicalCloneDirtyConvergeEffect

    def factory(
        _root: Path, *, env: object, delivery: ProtocolAlarmDelivery | None
    ) -> HandlerCanonicalCloneDirtyConvergeEffect:
        handler = built(
            clean_case.home,
            env=clean_case.env(**{AGE_ENV: str(AGE)}),
            engine_argv=clean_case.engine_argv,
            delivery=delivery,
        )
        clean_case.refuse_for(handler, 3 * AGE)
        return handler

    monkeypatch.setattr(
        handler_module, "HandlerCanonicalCloneDirtyConvergeEffect", factory
    )
    monkeypatch.setenv("OMNI_HOME", str(clean_case.home))
    result = CliRunner().invoke(dirty_converge_command, [])
    assert result.exit_code == code, result.output
    assert "REFUSED" in result.output
    assert ("[alarm: delivered]" in result.output) is alarmed


# -- the seams ------------------------------------------------------------------------------------
#
# Seam 1, refusal timer to converge action: the engine's REFUSED_TOO_LONG line is the only thing the
# handler reads, and the bound it hands the engine is the age at which the engine starts naming a
# clone. Seam 2, converge refusal to the lab alarm delivery: the request the handler writes is read
# by the real delivery handler (in-memory backend here; the runtime backend over the broker in the
# lab proof). Each fails when either side changes shape.

ENGINE_LINES = {
    "tracked changes": "REFUSED_TOO_LONG /reg/platform refused for 1800s: 2 staged path(s), e.g. a.txt, b.txt",
    "detached": "REFUSED_TOO_LONG /reg/platform refused for 180s: detached HEAD: the clone tracks no branch",
    "bare": "REFUSED_TOO_LONG /reg/platform refused for 600s: core.bare=true",
    "ahead": "REFUSED_TOO_LONG /reg/platform refused for 181s: dev and origin/dev have diverged",
}


@pytest.mark.parametrize("shape", list(ENGINE_LINES))
def test_seam_the_engines_breach_line_shape_is_the_one_the_handler_reads(
    shape: str,
) -> None:
    match = _BREACH.match(ENGINE_LINES[shape])
    assert match is not None
    assert match["clone"] == "/reg/platform"
    assert int(match["age"]) > 0


def test_seam_the_bound_the_handler_hands_the_engine_is_the_age_it_acts_at(
    clean_case: Planted, tmp_path: Path
) -> None:
    seen = tmp_path / "bound.txt"
    handler = clean_case.handler()
    handler._engine_argv = [
        "/bin/sh",
        "-c",
        f'printf "%s" "${ENGINE_BOUND_ENV}" > "{seen}"',
    ]
    handler.tick()
    assert int(seen.read_text()) == int(min(handler.age, handler.detached_age))
    assert int(seen.read_text()) == CONTRACT_BOUND


def _real_delivery() -> tuple[
    HandlerLabAlarmDeliver, InMemoryAlarmChannel, InMemoryAlarmLedger
]:
    overlay = ModelDeliveryOverlay.model_validate(
        yaml.safe_load(
            (
                Path(handler_module.__file__).parents[2]
                / "node_lab_alarm_deliver_effect"
                / "overlay.example.yaml"
            ).read_text()
        )
    )
    channel, ledger = InMemoryAlarmChannel(), InMemoryAlarmLedger()
    clock = [datetime(2026, 11, 13, 20, 0, tzinfo=UTC)]
    handler = HandlerLabAlarmDeliver(
        overlay=overlay,
        channel=channel,
        ledger=ledger,
        state=InMemoryDeliveryState(),
        clock=lambda: clock[0],
    )
    return handler, channel, ledger


def test_seam_the_real_alarm_delivery_posts_and_writes_one_row_per_episode(
    clean_case: Planted,
) -> None:
    delivery, channel, ledger = _real_delivery()
    name = _plant_staged(clean_case.clone)
    (first,) = _refused_tick(clean_case, delivery)
    assert first.alarm == "delivered"
    ((_category, text),) = channel.posts
    assert "clone_local_work_refused" in text
    assert str(clean_case.clone) in text
    ((_, row),) = ledger.rows
    assert "| FRICTION |" in row
    assert "condition=clone_local_work_refused" in row
    assert f"subject={clean_case.clone}" in row
    assert f"staged 1 ({name})" in row
    # still refused on the next tick: the episode is open, so nothing is posted again
    (again,) = _refused_tick(clean_case, delivery)
    assert again.status is EnumDirtyConvergeStatus.REFUSED
    assert again.alarm == "suppressed"
    assert len(channel.posts) == 1
    assert len(ledger.rows) == 1


def test_seam_an_alarm_episode_ends_when_the_clone_is_clean_and_a_return_raises_again(
    clean_case: Planted,
) -> None:
    delivery, channel, _ledger = _real_delivery()
    _plant_untracked(clean_case.clone)
    _refused_tick(clean_case, delivery)
    (clean_case.clone / "notes/kept.md").unlink()
    (cleared,) = _refused_tick(
        clean_case, delivery
    )  # no local work: OK ends the episode
    assert cleared.status is EnumDirtyConvergeStatus.SKIPPED
    _plant_modified(clean_case.clone)
    (raised,) = _refused_tick(clean_case, delivery)
    assert raised.alarm == "delivered"
    assert len(channel.posts) == 2


def test_seam_the_real_delivery_refuses_a_request_the_handler_could_not_have_written(
    clean_case: Planted,
) -> None:
    """Control: a request that names the wrong condition for its alarm is refused by the shared model."""
    with pytest.raises(ValidationError):
        ModelLabAlarmDeliveryRequest(
            correlation_id="c",
            condition="clone_local_work_refused",
            outcome=EnumDeliveryCondition.ALARM,
            alarms=(ModelLabAlarm(condition="other", subject="s", detail="d"),),
        )
