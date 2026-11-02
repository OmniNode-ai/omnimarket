# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21940: five validators this repo used to copy now run from omnibase_core.

Five hook ids that ran a repository script resolve to the omnibase_core hook
repository, and their scripts are gone. The script paths are spelled in parts
below so that this file is not itself a hit for the ``git grep`` that proves the
copies are gone.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_HOOK_REPO = "https://github.com/OmniNode-ai/omnibase_core"

# hook id -> (directory of the deleted script, its stem)
_CONSUMED_FROM_CORE: dict[str, tuple[str, str]] = {
    "exposed-identifier-gate": ("scripts/validation", "check_exposed" + "_identifiers"),
    "check-release-identity": ("scripts", "check_release" + "_identity"),
    "skip-count-ratchet": ("scripts/ci", "skip_count" + "_ratchet"),
    "integration-skip-guard": ("scripts/ci", "check_integration" + "_skips"),
    "precommit-interpreter-resolution": (
        "scripts/validation",
        "validate_precommit" + "_interpreter",
    ),
}
DELETED_SCRIPTS = {
    hook_id: f"{directory}/{stem}.py"
    for hook_id, (directory, stem) in _CONSUMED_FROM_CORE.items()
}


def _hook_repos() -> dict[str, list[str]]:
    config = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    found: dict[str, list[str]] = {}
    for repo in config["repos"]:
        for hook in repo.get("hooks") or []:
            found.setdefault(hook["id"], []).append(str(repo["repo"]))
    return found


# A verbatim, pinned census of check_values read from other repositories at fixed
# SHAs ("GENERATED DATA, DO NOT HAND-EDIT"): two of its strings name the
# omnibase_infra copy of one script by URL. They are the artifact under test of
# that census, not a call of this repository's script, and cannot be rewritten.
VERBATIM_CAPTURES = (
    ":(exclude)tests/unit/nodes/node_dod_verify/omn_15597_occ_census_pinned.py"
)


def _git_grep(pattern: str) -> list[str]:
    result = subprocess.run(
        ["git", "grep", "-n", "-F", "-e", pattern, "--", ".", VERBATIM_CAPTURES],
        cwd=REPO_ROOT,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    return result.stdout.splitlines()


@pytest.mark.parametrize("hook_id", sorted(DELETED_SCRIPTS))
def test_hook_id_resolves_to_the_core_hook_repo(hook_id: str) -> None:
    repos = _hook_repos().get(hook_id)
    assert repos == [CORE_HOOK_REPO], (
        f"{hook_id} must be declared exactly once, under {CORE_HOOK_REPO}; got {repos}"
    )


@pytest.mark.parametrize("hook_id", sorted(DELETED_SCRIPTS))
def test_the_deleted_copy_is_gone_and_unreferenced(hook_id: str) -> None:
    script = DELETED_SCRIPTS[hook_id]
    assert not (REPO_ROOT / script).exists(), f"{script} is a copy of a core hook"
    assert _git_grep(script) == []
    stem = _CONSUMED_FROM_CORE[hook_id][1]
    assert _git_grep(stem + ".py") == []


def test_a_surviving_local_hook_is_seen_by_the_parser() -> None:
    """Positive control: the parser sees local hooks, so the checks above can fail."""
    assert _hook_repos().get("leaked-literals-gate") == ["local"]
