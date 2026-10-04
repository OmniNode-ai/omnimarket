# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The omnibase_core pins move to the temp-file blob read fix (OMN-20460)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[2]
PRECOMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

NEW_CORE_REV = "f50d00fc7918d0b12ccd0d3497160325e32e7ac5"
OLD_CORE_REV = "7f20b97895f01996c5858dde385ba8991c06e708"
CORE_REPO_SUFFIX = "omnibase_core"
FILE_SHAPE_HOOK_ID = "canonical-file-shape"

CORE_PIN = re.compile(r"omnibase_core@([0-9a-f]{7,40})")


def _hook_ids(repo: dict[object, object]) -> list[object]:
    hooks = repo.get("hooks")
    assert isinstance(hooks, list), "expected a YAML list of hooks"
    return [hook.get("id") for hook in hooks if isinstance(hook, dict)]


def _core_hook_revs() -> list[str]:
    config = yaml.safe_load(PRECOMMIT_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(config, dict), "expected a YAML mapping"
    repos = config["repos"]
    assert isinstance(repos, list), "expected a YAML list of repos"
    revs: list[str] = []
    for repo in repos:
        assert isinstance(repo, dict), "expected a YAML mapping per repo"
        url = repo.get("repo")
        rev = repo.get("rev")
        if (
            isinstance(url, str)
            and url.rstrip("/").removesuffix(".git").endswith(CORE_REPO_SUFFIX)
            and isinstance(rev, str)
            and FILE_SHAPE_HOOK_ID in _hook_ids(repo)
        ):
            revs.append(rev)
    return revs


def test_precommit_core_hook_rev_is_new_rev() -> None:
    assert _core_hook_revs() == [NEW_CORE_REV]


def test_ci_core_pins_are_all_new_rev() -> None:
    pins = CORE_PIN.findall(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert pins, "ci.yml carries no omnibase_core@<sha> pin"
    assert set(pins) == {NEW_CORE_REV}


@pytest.mark.parametrize("path", [PRECOMMIT_CONFIG, CI_WORKFLOW], ids=lambda p: p.name)
def test_old_core_rev_is_gone(path: Path) -> None:
    assert OLD_CORE_REV not in path.read_text(encoding="utf-8")
