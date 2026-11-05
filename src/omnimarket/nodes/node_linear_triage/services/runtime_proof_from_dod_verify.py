# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The triage closer's default runtime-proof source: the dod_verify verdict (OMN-19998).

A merged ticket closes only on proof that its change runs
(:mod:`omnimarket.handlers.runtime_proof_close_rules`). The handler had no source of that
proof unless a caller injected one, so every merged ticket was held as "merged, not yet
proven running" even when its dod_verify PASS had read the running system.

This module reads that proof from the verdict the Done-write gate already runs:

* A verdict is a PASS only when its ``status`` is ``verified``.
* A check exercises the running system only when it is ``verified`` and its
  ``proof_class`` is ``readback`` (it read live state from a running system and asserted
  on it). A ``behavior`` check runs tests or the CLI from a clone, not the running system,
  so it is not runtime proof here.
* The receipt's revision is the ``head_sha`` of the product clone that readback ran in,
  and only a clone whose ``origin`` names the merged PR's repository is read.
* The merged change is the squash commit ``(#<n>)`` of the PR, found in that clone.
  Whether the receipt revision contains it is decided by
  :func:`evaluate_proof_close` with ``git merge-base --is-ancestor`` in that clone.

Fail-closed everywhere: no verdict is no proof; a clone, remote or commit that cannot be
read leaves the ticket held with the reason the rule gives.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.handlers.runtime_proof_close_rules import (
    GIT_TIMEOUT_SECONDS,
    GitRevisionContainment,
    evaluate_proof_close,
)
from omnimarket.models.runtime_proof.model_runtime_proof_close import (
    EnumRuntimeReceiptKind,
    ModelProofCloseDecision,
    ModelProofCloseRequest,
    ModelRuntimeReceipt,
)
from omnimarket.nodes.node_linear_triage.services.done_write_receipt_gate import (
    DodVerdictProbe,
)

_PASS = "verified"
_RUNNING_SYSTEM_PROOF_CLASS = "readback"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REMOTE_REPO = re.compile(r"[/:](?P<owner>[^/:]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")
_DEFAULT_BRANCH_REFS = ("refs/remotes/origin/HEAD", "refs/remotes/origin/dev")


class CachedDodVerdictProbe:
    """One verifier run per ticket per triage pass.

    The runtime-proof source and the Done-write gate both read the same verdict; the
    verifier executes the contract's checks and is slow, so it runs once.
    """

    def __init__(self, inner: DodVerdictProbe) -> None:
        self._inner = inner
        self._seen: dict[str, tuple[dict[str, object] | None, str]] = {}

    def clear(self) -> None:
        """Forget every verdict; a new triage pass reads each one fresh."""
        self._seen.clear()

    def verdict_for(self, *, ticket_id: str) -> tuple[dict[str, object] | None, str]:
        if ticket_id not in self._seen:
            self._seen[ticket_id] = self._inner.verdict_for(ticket_id=ticket_id)
        return self._seen[ticket_id]


def _git(clone: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", clone, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=scrub_git_location_env(os.environ),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _clone_repo_name(clone: str) -> str | None:
    url = _git(clone, "remote", "get-url", "origin")
    if url is None:
        return None
    match = _REMOTE_REPO.search(url.strip())
    return match.group("repo") if match else None


def _squash_commit(clone: str, pr_number: str, start_points: tuple[str, ...]) -> str:
    """The commit whose subject ends ``(#<pr_number>)``, first found from ``start_points``."""
    suffix = f"(#{pr_number})"
    for start in start_points:
        out = _git(clone, "log", "--first-parent", "--format=%H%x1f%s", start)
        for line in (out or "").splitlines():
            sha, _, subject = line.partition("\x1f")
            if subject.rstrip().endswith(suffix) and _SHA.match(sha):
                return sha
    return ""


def _running_system_clones(verdict: Mapping[str, object]) -> list[tuple[str, str]]:
    """``(repo_root, head_sha)`` of every verified readback check's product clone."""
    if str(verdict.get("status", "")).lower() != _PASS:
        return []
    found: list[tuple[str, str]] = []
    checks = verdict.get("checks")
    for check in checks if isinstance(checks, list) else []:
        if not isinstance(check, Mapping):
            continue
        if str(check.get("status", "")).lower() != _PASS:
            continue
        if str(check.get("proof_class", "")).lower() != _RUNNING_SYSTEM_PROOF_CLASS:
            continue
        clones = check.get("product_clones")
        for clone in clones if isinstance(clones, list) else []:
            if not isinstance(clone, Mapping):
                continue
            root = str(clone.get("repo_root") or "").strip()
            head = str(clone.get("head_sha") or "").strip().lower()
            if root and _SHA.match(head) and (root, head) not in found:
                found.append((root, head))
    return found


class DodVerifyRuntimeProofProbe:
    """:class:`RuntimeProofProbe` read from the ticket's dod_verify verdict."""

    def __init__(self, verdicts: DodVerdictProbe) -> None:
        self._verdicts = verdicts

    def proof_for(
        self, *, ticket_id: str, merged_pr: Mapping[str, str]
    ) -> ModelProofCloseDecision | None:
        verdict, _ = self._verdicts.verdict_for(ticket_id=ticket_id)
        if verdict is None:
            return None
        repo = str(merged_pr.get("repo") or "").strip()
        number = str(merged_pr.get("number") or "").strip()
        clones = [
            (root, head)
            for root, head in _running_system_clones(verdict)
            if repo and Path(root).is_dir() and _clone_repo_name(root) == repo
        ]
        if not clones:
            return evaluate_proof_close(
                ModelProofCloseRequest(ticket_id=ticket_id, change_merged=True)
            )
        root = clones[0][0]
        change_sha = (
            _squash_commit(
                root, number, tuple(h for _, h in clones) + _DEFAULT_BRANCH_REFS
            )
            if number.isdigit()
            else ""
        )
        return evaluate_proof_close(
            ModelProofCloseRequest(
                ticket_id=ticket_id,
                change_merged=True,
                change_sha=change_sha,
                receipts=tuple(
                    ModelRuntimeReceipt(
                        kind=EnumRuntimeReceiptKind.DOD_VERIFY_PASS,
                        passed=True,
                        revision=head,
                        exercises_running_system=True,
                    )
                    for r, head in clones
                    if r == root
                ),
                repo_path=root,
            ),
            GitRevisionContainment(root),
        )


__all__ = ["CachedDodVerdictProbe", "DodVerifyRuntimeProofProbe"]
