# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14941: unconditional forbidden-surface deny in ``_assert_append_only``.

``allowed_paths`` is derived from the compute plan's ``companion_files``, so
before this hardening a buggy/adversarial plan that listed
``grants/prod_promotion_grants.yaml`` (the OMN-13418 prod-promotion gate's
source of truth) or an ``allowlists/**`` file would sail through the membership
check — plan membership acted as authorization. These RED tests prove the deny
fires DESPITE the path being present in ``allowed_paths``, and that the
``contracts/`` + ``drift/`` evidence roots still pass.

Deletion + outside-allowed-set + merged-receipt-mutation coverage already lives
in ``test_append_only_guard_omn_14785.py`` (do-not-duplicate); this file covers
only the OMN-14941 delta. Driven against a REAL temporary git repo, extending
that file's pattern (including the autouse GIT_* env-clearing fixture —
required per reference_git_env_vars_override_c_and_cwd).
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


def _init_repo_with_grant(tmp_path: Path) -> str:
    """Init a repo carrying one committed prod-promotion grant; return base SHA."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "config", "user.email", "test@omninode.ai")
    grant = tmp_path / "grants" / "prod_promotion_grants.yaml"
    grant.parent.mkdir(parents=True, exist_ok=True)
    grant.write_text("grants: []\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base grant file")
    return _git(tmp_path, "rev-parse", "HEAD")


def _add_and_commit(tmp_path: Path, rel: str, content: str) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", f"touch {rel}")


@pytest.mark.unit
class TestForbiddenSurfaceDeny:
    """Plan membership must NEVER authorize a write outside contracts/ + drift/."""

    def test_modifying_the_prod_promotion_grant_is_rejected_despite_plan_membership(
        self, tmp_path: Path
    ) -> None:
        """The exact escalation vector: the plan lists the OMN-13418 grant file
        in companion_files (so it IS in allowed_paths) and the tree modifies it.
        The unconditional deny must still fire."""
        base = _init_repo_with_grant(tmp_path)
        grant_rel = "grants/prod_promotion_grants.yaml"
        _add_and_commit(tmp_path, grant_rel, "grants:\n  - forged\n")
        with pytest.raises(RuntimeError, match="forbidden change-control surface"):
            HandlerOccCompanionEffect()._assert_append_only(
                str(tmp_path), base, {grant_rel}
            )

    def test_adding_a_grants_file_is_rejected_despite_plan_membership(
        self, tmp_path: Path
    ) -> None:
        base = _init_repo_with_grant(tmp_path)
        new_grant_rel = "grants/new_grant.yaml"
        _add_and_commit(tmp_path, new_grant_rel, "grant: forged\n")
        with pytest.raises(RuntimeError, match="forbidden change-control surface"):
            HandlerOccCompanionEffect()._assert_append_only(
                str(tmp_path), base, {new_grant_rel}
            )

    def test_adding_an_allowlist_is_rejected_despite_plan_membership(
        self, tmp_path: Path
    ) -> None:
        base = _init_repo_with_grant(tmp_path)
        allowlist_rel = "allowlists/raw_prod_bypass.yaml"
        _add_and_commit(tmp_path, allowlist_rel, "allow: everything\n")
        with pytest.raises(RuntimeError, match="forbidden change-control surface"):
            HandlerOccCompanionEffect()._assert_append_only(
                str(tmp_path), base, {allowlist_rel}
            )

    def test_path_outside_contracts_and_drift_is_rejected_despite_plan_membership(
        self, tmp_path: Path
    ) -> None:
        """Not just grants//allowlists/: ANY root outside contracts/ + drift/ is
        denied even when the plan lists it (e.g. a workflow or script)."""
        base = _init_repo_with_grant(tmp_path)
        script_rel = "scripts/evil_hook.py"
        _add_and_commit(tmp_path, script_rel, "print('pwn')\n")
        with pytest.raises(RuntimeError, match="outside the contracts//drift/"):
            HandlerOccCompanionEffect()._assert_append_only(
                str(tmp_path), base, {script_rel}
            )

    def test_contracts_and_drift_adds_in_allowed_set_still_pass(
        self, tmp_path: Path
    ) -> None:
        """GREEN control: the legitimate companion shape (a contract + a receipt,
        both in allowed_paths) is unaffected by the hardening."""
        base = _init_repo_with_grant(tmp_path)
        contract_rel = "contracts/OMN-14941.yaml"
        receipt_rel = "drift/dod_receipts/OMN-14941/dod-001/command.yaml"
        _add_and_commit(tmp_path, contract_rel, "ticket_id: OMN-14941\n")
        _add_and_commit(tmp_path, receipt_rel, "status: PASS\n")
        # No raise.
        HandlerOccCompanionEffect()._assert_append_only(
            str(tmp_path), base, {contract_rel, receipt_rel}
        )

    def test_deleting_a_grant_is_still_a_deletion_violation(
        self, tmp_path: Path
    ) -> None:
        """Deletions stay first-class violations (the OMN-14785 rule) even on the
        forbidden surface — the message names the deletion."""
        base = _init_repo_with_grant(tmp_path)
        (tmp_path / "grants" / "prod_promotion_grants.yaml").unlink()
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", "delete grant")
        with pytest.raises(RuntimeError, match="deletes"):
            HandlerOccCompanionEffect()._assert_append_only(
                str(tmp_path), base, {"grants/prod_promotion_grants.yaml"}
            )
