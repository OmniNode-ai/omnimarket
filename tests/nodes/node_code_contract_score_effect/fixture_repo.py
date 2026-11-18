# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixture repository for node_code_contract_score_effect (OMN-23626).

A real git repository with a base commit holding a bug (``add`` subtracts) and a merge commit holding the fix, the check file
(``tests/test_add.py``) and the ticket contract that binds the check. The known-good draft is the merge's change to the
source file only; the check file and the contract are what the scorer pins by sha256 and places itself.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import textwrap
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from omnimarket.nodes.node_code_contract_score_effect.models import (
    ModelCodeScoreCheckFile,
    ModelCodeScoreContractRef,
    ModelCodeScoreOverlay,
    ModelCodeScoreRequest,
    ModelCodeScoreTry,
)

REPO = "fixture/repo"
CONTRACT_PATH = "contracts/OMN-1.yaml"
CHECK_PATH = "tests/test_add.py"
SOURCE_PATH = "calc.py"
SOURCE_FIXED = "def add(a, b):\n    return a + b\n"
SOURCE_BUG = "def add(a, b):\n    return a - b\n"
TEST_TEXT = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def contract_text(command: str | None = None, check_type: str = "test_passes") -> str:
    cmd = command or f"{sys.executable} -m pytest -q -p no:cacheprovider {CHECK_PATH}"
    return textwrap.dedent(
        f"""\
        schema_version: 1.0.0
        ticket_id: OMN-1
        title: add adds
        dod_evidence:
        - id: dod-1
          description: add returns the sum
          binds_ac:
          - AC1
          checks:
          - check_type: {check_type}
            check_value: "{cmd}"
            cwd: .
        """
    )


_GIT_LOCATION_ENV = frozenset(
    {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}
)


def scrub_git_location_env(env: Mapping[str, str]) -> dict[str, str]:
    """Drop the git location variables before shelling out to git (OMN-14891)."""
    scrubbed = {k: v for k, v in env.items() if k not in _GIT_LOCATION_ENV}
    scrubbed["GIT_CONFIG_NOSYSTEM"] = "1"
    return scrubbed


def sha(data: str | bytes) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


@dataclass(frozen=True)
class Fixture:
    root: Path
    scratch: Path
    base: str
    merge: str
    good_diff: str


def build_repo(tmp_path: Path, contract: str | None = None) -> Fixture:
    root = tmp_path / "clone"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "fixture@example.test")
    git(root, "config", "user.name", "fixture")
    (root / SOURCE_PATH).write_text(SOURCE_BUG)
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    (root / SOURCE_PATH).write_text(SOURCE_FIXED)
    (root / "tests").mkdir()
    (root / CHECK_PATH).write_text(TEST_TEXT)
    (root / "contracts").mkdir()
    (root / CONTRACT_PATH).write_text(contract or contract_text())
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "merge")
    merge = git(root, "rev-parse", "HEAD")
    good_diff = git(root, "diff", base, merge, "--", SOURCE_PATH) + "\n"
    return Fixture(root, tmp_path / "scratch", base, merge, good_diff)


def overlay(fx: Fixture, **over: object) -> ModelCodeScoreOverlay:
    values: dict[str, object] = {
        "check_timeout_s": 120,
        "env_sync_timeout_s": 60,
        "runnable_check_types": ("test_passes", "command"),
        "env_sync_command": "true",
        "scratch_root": str(fx.scratch),
        "repo_roots": {REPO: str(fx.root)},
        "protected_paths": {REPO: ("contracts/", ".github/")},
    }
    values.update(over)
    return ModelCodeScoreOverlay.model_validate(values)


def request(
    fx: Fixture,
    diff: str,
    *,
    contract: str | None = None,
    check_sha: str | None = None,
    contract_sha: str | None = None,
    item_ids: tuple[str, ...] = (),
    overlay_over: dict[str, object] | None = None,
    **over: object,
) -> ModelCodeScoreRequest:
    text = contract if contract is not None else contract_text()
    return ModelCodeScoreRequest(
        correlation_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        repo=REPO,
        base_commit=fx.base,
        draft_diff=diff,
        contract=ModelCodeScoreContractRef(
            repository=REPO,
            path=CONTRACT_PATH,
            revision=fx.merge,
            sha256=contract_sha or sha(text),
        ),
        check_files=(
            ModelCodeScoreCheckFile(
                path=CHECK_PATH, sha256=check_sha or sha(TEST_TEXT), revision=fx.merge
            ),
        ),
        check_item_ids=item_ids,
        try_identity=ModelCodeScoreTry(task_key="fixture-1", try_ordinal=1),
        gate_accepted=True,
        overlay=overlay(fx, **(overlay_over or {})),
        **over,
    )


def edit_diff(fx: Fixture, path: str, before: str, after: str) -> str:
    """A diff against the base that edits ``path`` (made in a throwaway clone, never the fixture's)."""
    work = fx.root.parent / "diffwork"
    if not work.exists():
        subprocess.run(
            ["git", "clone", "-q", str(fx.root), str(work)],
            check=True,
            env=scrub_git_location_env(os.environ),
        )
    git(work, "checkout", "-q", "--detach", fx.base)
    (work / path).parent.mkdir(parents=True, exist_ok=True)
    (work / path).write_text(after)
    git(work, "add", "-N", path)
    out = git(work, "diff", "--", path) + "\n"
    git(work, "reset", "-q", "--hard")
    git(work, "clean", "-fdq")
    return out
