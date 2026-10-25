# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure decisions for queued CLAIM mirroring and handoff retries (OMN-20864)."""

import re

from ..models import ModelRemoteLaneHandoffRequest, ModelRemoteLaneHandoffResult

_REQUEST_ID = re.compile(r"\breq=([0-9A-Za-z-]+)")


def _matching_claim(raw: str, stamp: str, lane: str) -> str | None:
    row = raw.rstrip("\r\n")
    if "\r" in row or "\n" in row:
        return None
    if row.startswith(f"{stamp} | CLAIM | ") and f"lane={lane}" in row.split(" | ")[2:]:
        return row
    return None


def _claim_mirror(
    request: ModelRemoteLaneHandoffRequest,
) -> ModelRemoteLaneHandoffResult:
    stamp = request.claim_stamp
    if not stamp:
        return ModelRemoteLaneHandoffResult(
            decision=request.decision, reason="no claim_stamp; cannot locate the CLAIM"
        )
    for raw in reversed(request.ledger_rows):
        row = _matching_claim(raw, stamp, request.lane)
        if row is not None:
            return ModelRemoteLaneHandoffResult(
                decision=request.decision,
                claim_row=row,
                claim_source="ledger",
                start_lane=True,
                reason=f"CLAIM of {request.lane} at {stamp} found in the ledger tail",
            )
    queued = (
        "queued=host-bus-outbox" in request.claim_output
        or "status=pending" in request.claim_output
    )
    match = _REQUEST_ID.search(request.claim_output) if queued else None
    if match is None:
        return ModelRemoteLaneHandoffResult(
            decision=request.decision,
            reason="CLAIM not in the ledger tail and no queued request id",
        )
    request_id = match.group(1)
    found_request = False
    for outbox_request in request.outbox_requests:
        if outbox_request.request_id != request_id:
            continue
        found_request = True
        # Split only at LF so an embedded CR cannot turn a malformed row into a CLAIM.
        for raw in reversed(outbox_request.payload.split("\n")):
            row = _matching_claim(raw, stamp, request.lane)
            if row is not None:
                return ModelRemoteLaneHandoffResult(
                    decision=request.decision,
                    claim_row=row,
                    claim_source="host-bus-outbox",
                    start_lane=True,
                    reason=f"CLAIM found in host-bus-outbox request {request_id}",
                )
    reason = (
        f"request {request_id} holds no CLAIM of {request.lane} at {stamp}"
        if found_request
        else f"queued request {request_id} not in the outbox"
    )
    return ModelRemoteLaneHandoffResult(decision=request.decision, reason=reason)


def _handoff_retry(
    request: ModelRemoteLaneHandoffRequest,
) -> ModelRemoteLaneHandoffResult:
    exit_code = request.handoff_exit
    if exit_code == 0:
        return ModelRemoteLaneHandoffResult(
            decision=request.decision,
            handoff_status="handed-off",
            reason="handoff exit 0: handed off",
        )
    retryable = False
    reason = f"handoff exit {exit_code}"
    if exit_code in {4, 5}:
        retryable = True
        reason += ": a later watcher tick can clear the handoff"
    elif exit_code == 3 and "no-claim" in request.handoff_output:
        if request.claim_on_ledger:
            reason += ": no-claim refusal with the CLAIM already on the live ledger"
        elif request.claim_stamp:
            retryable = True
            reason += (
                f": CLAIM at {request.claim_stamp} had not reached the live ledger; "
                "queued in the host bus outbox"
            )
        else:
            reason += ": no-claim refusal with no claim_stamp"
    else:
        reason += ": handoff refused"
    if request.final_attempt and retryable:
        reason += "; retry window is over"
    return ModelRemoteLaneHandoffResult(
        decision=request.decision,
        handoff_status="pending"
        if retryable and not request.final_attempt
        else "refused",
        reason=reason,
    )


class HandlerRemoteLaneHandoff:
    """Use supplied facts to decide whether to start a lane or retry its handoff."""

    def handle(
        self, request: ModelRemoteLaneHandoffRequest
    ) -> ModelRemoteLaneHandoffResult:
        if request.decision == "claim_mirror":
            return _claim_mirror(request)
        return _handoff_retry(request)
