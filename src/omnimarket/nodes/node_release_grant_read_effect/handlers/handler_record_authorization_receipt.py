# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Record merge authorization only after a fresh, recorded grant read."""

from __future__ import annotations

from omnimarket.models.release_gate.model_release_grant_facts import (
    EnumGateBoundary,
    EnumGrantState,
    ModelReleaseAuthorizationReceipt,
)
from omnimarket.nodes.node_release_grant_read_effect.handlers.handler_read_release_grant import (
    HandlerReadReleaseGrant,
)
from omnimarket.nodes.node_release_grant_read_effect.models.model_release_grant import (
    ModelReadReleaseGrantRequest,
    ModelRecordAuthorizationReceiptRequest,
    ModelRecordAuthorizationReceiptResult,
)
from omnimarket.protocols.protocol_change_source import ProtocolChangeSource
from omnimarket.protocols.protocol_clock import ProtocolClock
from omnimarket.protocols.protocol_work_ledger_store import ProtocolWorkLedgerStore


def _receipt_fields(receipt: ModelReleaseAuthorizationReceipt) -> dict[str, str]:
    return {
        "repository": receipt.repository,
        "pull_request": str(receipt.pull_request),
        "head_sha": receipt.head_sha,
        "merge_commit_sha": receipt.merge_commit_sha,
        "merge_method": receipt.merge_method.value,
        "grant_event_id": receipt.grant_event_id,
        "policy_digest": receipt.policy_digest,
        "written_at": receipt.written_at.isoformat(),
    }


class HandlerRecordAuthorizationReceipt:
    """Bind the granted head to its merge commit through the append-only ledger."""

    def __init__(
        self,
        change_source: ProtocolChangeSource | None = None,
        ledger: ProtocolWorkLedgerStore | None = None,
        clock: ProtocolClock | None = None,
        reader: HandlerReadReleaseGrant | None = None,
    ) -> None:
        self._change_source = change_source
        self._ledger = ledger
        self._clock = clock
        self._reader = (
            reader
            if reader is not None
            else HandlerReadReleaseGrant(change_source, ledger, clock)
        )

    def handle(
        self, request: ModelRecordAuthorizationReceiptRequest
    ) -> ModelRecordAuthorizationReceiptResult:
        facts = self._reader.handle(
            ModelReadReleaseGrantRequest(
                repository=request.repository,
                pull_request=request.pull_request,
                head_sha=request.head_sha,
                boundary=EnumGateBoundary.MERGE,
                policy=request.policy,
            )
        )
        if self._change_source is None or self._ledger is None or self._clock is None:
            return ModelRecordAuthorizationReceiptResult(
                written=False, reason="ports_not_bound", facts=facts
            )
        if facts.state is not EnumGrantState.GRANTED:
            return ModelRecordAuthorizationReceiptResult(
                written=False, reason="no_grant_at_merge:" + facts.reason, facts=facts
            )
        assert facts.grant_event_id is not None
        receipt = ModelReleaseAuthorizationReceipt(
            repository=request.repository,
            pull_request=request.pull_request,
            head_sha=facts.head_sha,
            merge_commit_sha=request.merge_commit_sha,
            merge_method=request.merge_method,
            grant_event_id=facts.grant_event_id,
            policy_digest=facts.policy_digest,
            written_at=self._clock.now(),
        )
        try:
            self._ledger.append_row(
                "release_authorization_receipt", _receipt_fields(receipt)
            )
        except Exception:
            return ModelRecordAuthorizationReceiptResult(
                written=False, reason="receipt_unwritable", facts=facts
            )
        return ModelRecordAuthorizationReceiptResult(
            written=True, reason="receipt_written", facts=facts, receipt=receipt
        )
