# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure, deterministic fold for one pr-head lab-proof receipt (OMN-19566).

Canonical definition B (CLAUDE.md rule 7a): typed request in, typed result out,
no I/O, no clock. The effect-class writer beside it reads the stored row, calls
this fold, and persists what it returns.
"""

from __future__ import annotations

from omnimarket.nodes.node_projection_lab_proof_receipts.models import (
    ModelLabProofReceiptProjectionRequest,
    ModelLabProofReceiptProjectionResult,
    ModelLabProofReceiptRow,
)


class HandlerProjectionLabProofReceipts:
    """One row per receipt key; a later proof of the same key replaces it."""

    def handle(
        self, request: ModelLabProofReceiptProjectionRequest
    ) -> ModelLabProofReceiptProjectionResult:
        event = request.event
        previous = request.previous_row
        # A redelivery, or an older proof of the same key arriving late, never
        # replaces a newer one. finished_at orders proofs of one key: two runs
        # of one head under one profile differ in when they finished.
        if previous is not None and event.finished_at <= previous.finished_at:
            return ModelLabProofReceiptProjectionResult(row=previous, applied=False)
        row = ModelLabProofReceiptRow(
            repo=event.repo,
            pr_number=event.pr_number,
            head_sha=event.head_sha,
            profile_id=event.profile_id,
            profile_version=event.profile_version,
            receipt_key=event.receipt_key,
            handler_kind=event.handler_kind,
            result=event.result,
            verifier_token=event.verifier_token,
            verifier_reason=event.verifier_reason,
            mandatory_checks=event.mandatory_checks,
            missing_mandatory_checks=event.missing_mandatory_checks,
            failing_checks=event.failing_checks,
            started_at=event.started_at,
            finished_at=event.finished_at,
            runner_identity=event.runner_identity,
            verifier_identity=event.verifier_identity,
            host=event.host,
            slot=event.slot,
            carried_from=event.carried_from,
            receipt=event.receipt,
        )
        return ModelLabProofReceiptProjectionResult(row=row, applied=True)


__all__ = ["HandlerProjectionLabProofReceipts"]
