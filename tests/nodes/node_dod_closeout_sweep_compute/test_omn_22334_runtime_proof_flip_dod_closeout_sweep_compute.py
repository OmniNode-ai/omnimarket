# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22334: the closeout sweep flips a ticket Done only on runtime proof.

Every other conjunct of the flip predicate met and every PR merged is still not enough: the
change must be shown to run, by a passing runtime receipt at a revision that contains it.
Containment is read from a real git repository built here.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.events.dod_closeout_sweep import (
    EnumCloseoutAction,
    ModelDodCloseoutDecisionRequest,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.handler_dod_closeout_sweep import (
    BLOCK_MERGED_NOT_PROVEN_RUNNING,
    HandlerDodCloseoutSweep,
)

DATE = "2026-11-03"
FLIPPABLE: dict[str, Any] = {
    "ticket_id": "OMN-1",
    "total_checks": 6,
    "verified_count": 5,
    "non_probative_count": 1,
    "behavior_proving_count": 2,
    "criteria": ["AC1", "AC2", "AC3"],
    "bound_criteria": ["AC1", "AC2", "AC3"],
    "all_prs_merged": True,
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "f").write_text("0")
    _git(root, "add", "f")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "f").write_text("1")
    _git(root, "commit", "-qam", "change")
    change = _git(root, "rev-parse", "HEAD")
    (root / "f").write_text("2")
    _git(root, "commit", "-qam", "after")
    after = _git(root, "rev-parse", "HEAD")
    return {"path": str(root), "base": base, "change": change, "after": after}


def _proof(repo: dict[str, str], revision: str, **extra: Any) -> dict[str, Any]:
    return {
        "ticket_id": "OMN-1",
        "change_merged": True,
        "change_sha": repo["change"],
        "repo_path": repo["path"],
        "receipts": [
            {"kind": "lab_pass", "passed": True, "revision": revision, "host": "h201"}
        ],
        **extra,
    }


def flip(**overrides: Any) -> Any:
    request = ModelDodCloseoutDecisionRequest.model_validate(
        {"kind": "flip_decision", "date": DATE, **FLIPPABLE, **overrides}
    )
    return HandlerDodCloseoutSweep().handle(request)


def test_merged_only_does_not_close(repo: dict[str, str]) -> None:
    result = flip()
    assert result.action is EnumCloseoutAction.HELD_GAP
    assert result.predicate_met is False
    assert result.unmet == [BLOCK_MERGED_NOT_PROVEN_RUNNING]
    assert result.reason is not None
    assert result.reason.startswith("merged, not yet proven running")

    no_receipts = flip(runtime_proof={**_proof(repo, repo["after"]), "receipts": []})
    assert no_receipts.action is EnumCloseoutAction.HELD_GAP
    assert no_receipts.primary_blocker == BLOCK_MERGED_NOT_PROVEN_RUNNING


def test_merged_plus_runtime_receipt_closes(repo: dict[str, str]) -> None:
    result = flip(runtime_proof=_proof(repo, repo["after"]))
    assert result.action is EnumCloseoutAction.FLIPPED_DONE
    assert (result.predicate_met, result.unmet, result.primary_blocker) == (
        True,
        [],
        None,
    )
    # the receipt may sit exactly on the merge commit
    assert (
        flip(runtime_proof=_proof(repo, repo["change"])).action
        is EnumCloseoutAction.FLIPPED_DONE
    )


def test_receipt_without_change_does_not_close(repo: dict[str, str]) -> None:
    result = flip(runtime_proof=_proof(repo, repo["base"]))
    assert result.action is EnumCloseoutAction.HELD_GAP
    assert result.unmet == [BLOCK_MERGED_NOT_PROVEN_RUNNING]
    assert result.reason is not None
    assert repo["base"] in result.reason


def test_docs_or_plan_names_declared_proof(repo: dict[str, str]) -> None:
    for kind in ("docs", "plan"):
        base = {"ticket_kind": kind, "receipts": []}
        held = flip(runtime_proof={**_proof(repo, repo["after"]), **base})
        assert held.action is EnumCloseoutAction.HELD_GAP

        unmet = flip(
            runtime_proof={
                **_proof(repo, repo["after"]),
                **base,
                "declared_proof": {"name": "plan on the board", "satisfied": False},
            }
        )
        assert unmet.action is EnumCloseoutAction.HELD_GAP

        met = flip(
            runtime_proof={
                **_proof(repo, repo["after"]),
                **base,
                "declared_proof": {
                    "name": "plan on the board",
                    "satisfied": True,
                    "evidence": "board readback",
                },
            }
        )
        assert met.action is EnumCloseoutAction.FLIPPED_DONE


def test_merged_only_does_not_close_never_widens(repo: dict[str, str]) -> None:
    # runtime proof is one more conjunct: with another conjunct unmet it still holds
    result = flip(
        runtime_proof=_proof(repo, repo["after"]), bound_criteria=["AC1", "AC2"]
    )
    assert result.action is EnumCloseoutAction.HELD_GAP
    assert result.unmet == ["ac:unbound"]
    unmerged = flip(runtime_proof=_proof(repo, repo["after"]), all_prs_merged=False)
    assert unmerged.unmet == ["pr:unmerged"]
