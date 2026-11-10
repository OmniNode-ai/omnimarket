# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23542: the callers of the two repaired reusable workflows use the repaired versions.

omnibase_core#2306 (imperative-contract-guard) and omniclaude#3251 (kb-doc-gate) stopped the
validator checkout from calling the rate-limited default-branch API with an empty ref. The fix
reaches this repository's checks only when the caller pin names a commit that contains it, so
each caller must pin a dev-merge tag at or after the floor tag the fixture records. The
kb-doc-gate pre-commit hook must name the same commit as the caller's tag, so the local and CI
verdicts cannot disagree.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PRE_COMMIT = REPO_ROOT / ".pre-commit-config.yaml"
FLOOR = Path(__file__).resolve().parent / "fixtures" / "rate_limit_pin_floor.json"
_TAG = re.compile(r"dev-(\d{14})-([0-9a-f]{12})")
_SHA = re.compile(r"[0-9a-f]{40}")


def _callers() -> list[dict[str, Any]]:
    loaded = json.loads(FLOOR.read_text(encoding="utf-8"))
    callers = loaded["callers"]
    assert isinstance(callers, list)
    assert callers
    return callers


def tag_stamp(ref: str) -> str:
    """The 14-digit UTC stamp of a dev-merge tag; any other ref is refused."""
    match = _TAG.fullmatch(ref)
    assert match is not None, f"ref {ref!r} is not a dev-merge tag"
    return match.group(1)


def at_or_after(ref: str, floor_tag: str) -> bool:
    return tag_stamp(ref) >= tag_stamp(floor_tag)


def _pinned_ref(workflow: str, uses: str) -> str:
    lines = [
        line.strip()
        for line in (WORKFLOWS / workflow).read_text(encoding="utf-8").splitlines()
        if line.strip().startswith(f"uses: {uses}@")
    ]
    assert len(lines) == 1, f"{workflow} must hold exactly one uses line for {uses}"
    return lines[0].rpartition("@")[2].split()[0]


@pytest.mark.parametrize("caller", _callers(), ids=lambda c: c["workflow"])
def test_caller_pins_a_tag_at_or_after_the_fix(caller: dict[str, Any]) -> None:
    ref = _pinned_ref(caller["workflow"], caller["uses"])
    assert at_or_after(ref, caller["floor_tag"]), (
        f"{caller['workflow']} pins {ref}, before the tag that carries {caller['fix']}"
    )


@pytest.mark.parametrize(
    "caller", [c for c in _callers() if c["hook_repo"]], ids=lambda c: c["hook_id"]
)
def test_hook_rev_names_the_commit_of_the_callers_tag(caller: dict[str, Any]) -> None:
    ref = _pinned_ref(caller["workflow"], caller["uses"])
    sha12 = _TAG.fullmatch(ref).group(2)  # type: ignore[union-attr]
    config = yaml.safe_load(PRE_COMMIT.read_text(encoding="utf-8"))
    revs = [
        str(repo["rev"])
        for repo in config["repos"]
        if repo.get("repo") == caller["hook_repo"]
        and any(hook.get("id") == caller["hook_id"] for hook in repo["hooks"])
    ]
    assert len(revs) == 1, "exactly one repo block carries the hook"
    assert _SHA.fullmatch(revs[0]), f"hook rev {revs[0]!r} is not a full sha"
    assert revs[0].startswith(sha12), "hook rev and caller tag name different commits"


def test_error_chain_pre_fix_core_tag_is_refused() -> None:
    # The pin this repository held before OMN-23542 predates omnibase_core#2306.
    assert not at_or_after(
        "dev-20261018024445-b6ee996befa1", "dev-20261110125852-e6bc5e13e9e7"
    )


def test_error_chain_pre_fix_raw_sha_is_refused() -> None:
    # The kb-doc-gate caller held a raw sha: no stamp, so no proof it follows the fix.
    with pytest.raises(AssertionError, match="not a dev-merge tag"):
        tag_stamp("6bfba15f7d103ddaa9e949c459e95153b1ca6a94")


def test_error_chain_branch_ref_is_refused() -> None:
    with pytest.raises(AssertionError, match="not a dev-merge tag"):
        tag_stamp("main")
