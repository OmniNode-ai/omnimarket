# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14785 (parent OMN-14783): F-01 append-only guard parity for the write-EFFECT.

``OccCompanionEmitter`` fails CLOSED (``_assert_append_only``) before pushing if
the generated tree deletes anything or touches a path outside the run's
contract+receipt set — the guard that stops a generated companion from mutating
an already-merged receipt (OCC#4293/4295/4296). The canonical write-EFFECT
(``node_occ_companion_effect``) previously force-pushed with only an "all-adds"
comment; this ports the real ``git diff`` guard so the canonical producer is at
parity before the emitter is retired.

Driven against a REAL temporary git repo (the guard is a thin wrapper over
``git diff --name-status``), so the RED cases prove the guard actually fires.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect import (
    HandlerOccCompanionEffect,
)


@pytest.fixture(autouse=True)
def _clear_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear inherited GIT_* vars so git ops target the temp repo, not the real
    worktree (reference_git_env_vars_override_c_and_cwd — a pre-push hook exports
    GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE and would redirect these commands)."""
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(var, raising=False)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _init_repo_with_merged_receipt(tmp_path: Path) -> str:
    """Init a repo carrying one already-merged receipt; return the base SHA."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "config", "user.email", "test@omninode.ai")
    merged = tmp_path / "drift" / "dod_receipts" / "OMN-1" / "dod-001" / "command.yaml"
    merged.parent.mkdir(parents=True, exist_ok=True)
    merged.write_text("status: PASS\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base merged receipt")
    return _git(tmp_path, "rev-parse", "HEAD")


def _add_and_commit(tmp_path: Path, rel: str, content: str) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", f"add {rel}")


@pytest.mark.unit
class TestAppendOnlyGuard:
    def test_pure_adds_within_allowed_set_pass(self, tmp_path: Path) -> None:
        base = _init_repo_with_merged_receipt(tmp_path)
        _add_and_commit(tmp_path, "contracts/OMN-9999.yaml", "ticket_id: OMN-9999\n")
        # No raise — the only change is an add inside the allowed set.
        HandlerOccCompanionEffect()._assert_append_only(
            str(tmp_path), base, {"contracts/OMN-9999.yaml"}
        )

    def test_add_outside_allowed_set_is_rejected(self, tmp_path: Path) -> None:
        base = _init_repo_with_merged_receipt(tmp_path)
        _add_and_commit(tmp_path, "contracts/OMN-9999.yaml", "ticket_id: OMN-9999\n")
        _add_and_commit(tmp_path, "unexpected/foreign.yaml", "not: mine\n")
        with pytest.raises(RuntimeError, match="append-only violation"):
            HandlerOccCompanionEffect()._assert_append_only(
                str(tmp_path), base, {"contracts/OMN-9999.yaml"}
            )

    def test_deleting_a_merged_receipt_is_rejected(self, tmp_path: Path) -> None:
        base = _init_repo_with_merged_receipt(tmp_path)
        merged_rel = "drift/dod_receipts/OMN-1/dod-001/command.yaml"
        (tmp_path / merged_rel).unlink()
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", "delete merged receipt")
        with pytest.raises(RuntimeError, match="deletes"):
            HandlerOccCompanionEffect()._assert_append_only(
                str(tmp_path), base, {"contracts/OMN-9999.yaml"}
            )

    def test_renaming_a_merged_receipt_into_allowed_path_is_rejected(
        self, tmp_path: Path
    ) -> None:
        base = _init_repo_with_merged_receipt(tmp_path)
        _git(tmp_path, "config", "diff.renames", "true")
        merged_rel = "drift/dod_receipts/OMN-1/dod-001/command.yaml"
        allowed_new_rel = "drift/dod_receipts/OMN-9999/dod-new-receipt/command.yaml"
        (tmp_path / allowed_new_rel).parent.mkdir(parents=True, exist_ok=True)
        _git(tmp_path, "mv", merged_rel, allowed_new_rel)
        _git(tmp_path, "commit", "-q", "-m", "rename merged receipt")
        with pytest.raises(RuntimeError, match="deletes"):
            HandlerOccCompanionEffect()._assert_append_only(
                str(tmp_path), base, {allowed_new_rel}
            )

    def test_modifying_a_merged_receipt_is_rejected(self, tmp_path: Path) -> None:
        """The exact OCC#4293/4295/4296 failure mode: a generated companion that
        rewrites an already-merged receipt is rejected because that path is not
        in this run's allowed (net-new) set."""
        base = _init_repo_with_merged_receipt(tmp_path)
        merged_rel = "drift/dod_receipts/OMN-1/dod-001/command.yaml"
        (tmp_path / merged_rel).write_text("status: FAIL\n", encoding="utf-8")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", "mutate merged receipt")
        with pytest.raises(RuntimeError, match="append-only violation"):
            HandlerOccCompanionEffect()._assert_append_only(
                str(tmp_path), base, {"contracts/OMN-9999.yaml"}
            )
