# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A canonical clone refused for tracked changes converges on its own past a declared age (OMN-21821).

The planted clones are the two real cases of 2026-10-31: a host's omniclaude held back by staged
source files that had since merged upstream, and a host's omnidash held back by a stale generated
file. The age is the contract's, replaced by the overlay variable. By default the test drives a fake
engine and a fake converge script; with ``ONEX_TEST_OMNICLAUDE_ROOT`` naming a checkout of the
sync engine's repository it drives the real engine and the real script instead.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_canonical_clone_dirty_converge_effect.handlers.handler_canonical_clone_dirty_converge_effect import (
    AGE_ENV,
    HandlerCanonicalCloneDirtyConvergeEffect,
    declared_age_seconds,
    load_declaration,
)
from omnimarket.nodes.node_canonical_clone_dirty_converge_effect.models.model_canonical_clone_dirty_converge import (
    EnumDirtyConvergeStatus,
)

pytestmark = pytest.mark.unit

REAL_ROOT = os.environ.get("ONEX_TEST_OMNICLAUDE_ROOT", "")
HOST_VALUE = "/" + "Users" + "/someone/Code"  # built, so no home path sits in this file
AGE = 600


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


FAKE_CONVERGE = """#!/bin/bash
clone="$1"; shift
note=""
while [ $# -gt 0 ]; do case "$1" in --note) note="$2"; shift ;; esac; shift; done
git -C "$clone" reset --hard --quiet '@{u}'
echo "STATUS converged $clone saved: $note" >> "$ROW_FILE"
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
        (self.home / "omniclaude").symlink_to(Path(REAL_ROOT))

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
            ONEX_REGISTRY_ROOTS="",
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
    assert declared_age_seconds({}) == 14400
    assert declared_age_seconds({AGE_ENV: "90"}) == 90
    assert declared_age_seconds({AGE_ENV: "junk"}) == 14400
    assert load_declaration().rescue_branch_prefix == "rescue/"


def test_a_clone_not_yet_refused_for_the_age_is_left_alone(
    staged_case: Planted,
) -> None:
    handler = staged_case.handler(**{AGE_ENV: str(AGE)})
    staged_case.refuse_for(handler, 60)
    before = _git("rev-parse", "HEAD", cwd=staged_case.clone)
    assert handler.tick() == []
    assert _git("rev-parse", "HEAD", cwd=staged_case.clone) == before
    assert staged_case.branches() == ["dev"]


def test_staged_source_files_go_to_a_rescue_branch_then_the_clone_converges(
    staged_case: Planted,
) -> None:
    handler = staged_case.handler(**{AGE_ENV: str(AGE)})
    staged_case.refuse_for(handler, 3 * AGE)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.CONVERGED, outcome
    assert _git("rev-parse", "HEAD", cwd=staged_case.clone) == staged_case.tip
    assert _clean(staged_case.clone)
    (rescue,) = [b for b in staged_case.branches() if b.startswith("rescue/")]
    assert rescue == outcome.rescue_branch
    assert "platform-dirty-converge" in rescue
    assert (
        _git("show", f"{rescue}:a.txt", cwd=staged_case.remote) == "staged source edit"
    )
    row = staged_case.row_text()
    assert f"2 source path(s) pushed to origin branch {rescue}" in row


def test_a_generated_file_with_a_host_value_is_archived_and_never_pushed(
    generated_case: Planted,
) -> None:
    handler = generated_case.handler(**{AGE_ENV: str(AGE)})
    generated_case.refuse_for(handler, 3 * AGE)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.CONVERGED, outcome
    assert _clean(generated_case.clone)
    assert generated_case.branches() == ["dev"]
    assert outcome.archive_only_paths == {
        "src/data-source-defaults.ts": "carries a host value"
    }
    assert (
        "kept only in the converge archive, never pushed" in generated_case.row_text()
    )
    if REAL_ROOT:
        (archive,) = (
            generated_case.home / ".onex_state/canonical-clone-converge"
        ).glob("omnidash-*")
        assert HOST_VALUE in (archive / "full-vs-HEAD.patch").read_text()


def test_a_failed_rescue_push_leaves_the_clone_dirty(staged_case: Planted) -> None:
    _git(
        "remote",
        "set-url",
        "--push",
        "origin",
        str(staged_case.home / "missing.git"),
        cwd=staged_case.clone,
    )
    handler = staged_case.handler(**{AGE_ENV: str(AGE)})
    staged_case.refuse_for(handler, 3 * AGE)
    before = _git("rev-parse", "HEAD", cwd=staged_case.clone)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.FAILED
    assert "the rescue push failed" in outcome.reason
    assert _git("rev-parse", "HEAD", cwd=staged_case.clone) == before
    assert (
        _git("diff", "--cached", "--name-only", cwd=staged_case.clone) == "a.txt\nb.txt"
    )


def test_a_clone_ahead_of_its_upstream_is_never_converged(
    staged_case: Planted,
) -> None:
    clone = staged_case.clone
    _git("reset", "--soft", "HEAD", cwd=clone)
    (clone / "local.txt").write_text("local commit\n")
    _git("add", "local.txt", cwd=clone)
    _git("commit", "--quiet", "-m", "local", cwd=clone)
    handler = staged_case.handler(**{AGE_ENV: str(AGE)})
    staged_case.refuse_for(handler, 3 * AGE)
    (outcome,) = handler.tick()
    assert outcome.status is EnumDirtyConvergeStatus.SKIPPED
    assert "diverged" in outcome.reason
    assert (clone / "local.txt").exists()
