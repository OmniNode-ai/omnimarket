# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ticket closes only on runtime proof (OMN-22334).

Operator rulings 2026-11-02T13:31:33Z and 2026-11-02T13:36:01Z (lane orchestrator-31d9):
a merge is not done; the ticket's definition of done is read from evidence. This rule
is shared by every omnimarket path that writes Done on delivery evidence.

* A code ticket closes when its change is merged AND a passing runtime receipt (lab-pass,
  deploy-agent instance receipt, or a dod_verify PASS whose checks exercise the running
  system) was taken at a revision that contains the merged change. A merge with no such
  receipt, or a receipt at a revision lacking the change, is held with the reason
  ``merged, not yet proven running``.
* A docs-only or plan ticket closes when its change is merged AND the proof its own
  definition of done names is evidenced. Nothing is inferred when none is named.

The rule narrows the evidence a close accepts; it admits no new kind.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Protocol

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.models.runtime_proof.model_runtime_proof_close import (
    EnumProofAction,
    EnumProofTicketKind,
    EnumRuntimeReceiptKind,
    ModelProofCloseDecision,
    ModelProofCloseRequest,
    ModelRuntimeReceipt,
)

MERGED_NOT_PROVEN_RUNNING = "merged, not yet proven running"
GIT_TIMEOUT_SECONDS = 30.0


class RevisionContainment(Protocol):
    """Whether a revision contains a change. ``None`` when it cannot be read."""

    def contains(self, *, change_sha: str, revision: str) -> bool | None: ...


class GitRevisionContainment:
    """Containment read from a local clone: ``git merge-base --is-ancestor``."""

    def __init__(self, repo_path: Path | str) -> None:
        self._repo = str(repo_path)

    def contains(self, *, change_sha: str, revision: str) -> bool | None:
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    self._repo,
                    "merge-base",
                    "--is-ancestor",
                    change_sha,
                    revision,
                ],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
                env=scrub_git_location_env(os.environ),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        return None


def _held(action: EnumProofAction, reason: str) -> ModelProofCloseDecision:
    return ModelProofCloseDecision(allowed=False, action=action, reason=reason)


def _is_runtime_proof(receipt: ModelRuntimeReceipt) -> bool:
    if not receipt.passed or not receipt.revision.strip():
        return False
    if receipt.kind is EnumRuntimeReceiptKind.DOD_VERIFY_PASS:
        return receipt.exercises_running_system
    return True


def _evaluate_declared_proof(
    request: ModelProofCloseRequest,
) -> ModelProofCloseDecision:
    proof = request.declared_proof
    if proof is None or not proof.name.strip():
        return _held(
            EnumProofAction.HELD_DECLARED_PROOF_MISSING,
            f"merged, but this {request.ticket_kind.value} ticket's definition of done "
            "names no proof, so none can be accepted",
        )
    if not proof.satisfied or not proof.evidence.strip():
        return _held(
            EnumProofAction.HELD_MERGED_NOT_PROVEN_RUNNING,
            f"merged, declared proof '{proof.name.strip()}' not yet evidenced",
        )
    return ModelProofCloseDecision(
        allowed=True,
        action=EnumProofAction.CLOSE,
        reason=f"declared proof '{proof.name.strip()}' evidenced: {proof.evidence.strip()}",
    )


def evaluate_proof_close(
    request: ModelProofCloseRequest, containment: RevisionContainment | None = None
) -> ModelProofCloseDecision:
    """Whether the ticket closes on the evidence read. Fails closed at every step."""
    if not request.change_merged:
        return _held(
            EnumProofAction.HELD_NOT_MERGED, "the ticket's change is not merged"
        )
    if request.ticket_kind is not EnumProofTicketKind.CODE:
        return _evaluate_declared_proof(request)
    if not request.change_sha.strip():
        return _held(
            EnumProofAction.HELD_MERGED_NOT_PROVEN_RUNNING,
            f"{MERGED_NOT_PROVEN_RUNNING}: the merged change names no revision, so no "
            "receipt can be tied to it",
        )
    runtime = [r for r in request.receipts if _is_runtime_proof(r)]
    if not runtime:
        return _held(
            EnumProofAction.HELD_MERGED_NOT_PROVEN_RUNNING,
            f"{MERGED_NOT_PROVEN_RUNNING}: no passing runtime receipt (lab-pass, "
            "deploy-agent instance receipt, or a dod_verify PASS that exercises the "
            "running system)",
        )
    probe = containment or GitRevisionContainment(request.repo_path)
    lacking: list[str] = []
    unreadable: list[str] = []
    for receipt in runtime:
        contained = probe.contains(
            change_sha=request.change_sha, revision=receipt.revision
        )
        if contained is True:
            return ModelProofCloseDecision(
                allowed=True,
                action=EnumProofAction.CLOSE,
                reason=(
                    f"{receipt.kind.value} passed at {receipt.revision}, which contains "
                    f"the merged change {request.change_sha}"
                ),
                proving_receipt=receipt,
            )
        (unreadable if contained is None else lacking).append(receipt.revision)
    parts = []
    if lacking:
        parts.append("receipt revision lacks the merged change: " + ", ".join(lacking))
    if unreadable:
        parts.append("containment unreadable for: " + ", ".join(unreadable))
    return _held(
        EnumProofAction.HELD_MERGED_NOT_PROVEN_RUNNING,
        f"{MERGED_NOT_PROVEN_RUNNING}: " + "; ".join(parts),
    )
