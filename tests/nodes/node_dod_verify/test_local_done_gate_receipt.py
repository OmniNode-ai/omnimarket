# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A skipped local_done_gate item reads verified only on a matching bus receipt (OMN-26725)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest

from omnimarket.enums.enum_dod_contract_source import EnumDodContractSource
from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.models.model_dod_verify_state import EnumEvidenceCheckStatus
from omnimarket.models.model_local_done_gate_receipt import (
    ModelLocalDoneGateItemVerdict,
    ModelLocalDoneGateReceipt,
)
from omnimarket.models.model_local_done_gate_receipt_store import (
    FilesystemLocalDoneGateReceiptStore,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_contract_subject import (
    ModelDodContractSubject,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

TICKET = "OMN-26725"
SHA = "a" * 40
OTHER_SHA = "b" * 40
ITEM = "dod-local-integration-proof"
_NOW = datetime(2026, 11, 26, tzinfo=UTC)


_Verdict = Literal["VERIFIED", "FAILED", "SKIPPED", "NON_PROBATIVE"]


class _Source:
    def __init__(self, receipts: list[ModelLocalDoneGateReceipt]) -> None:
        self._receipts = receipts

    def receipts_for(self, ticket_id: str) -> list[ModelLocalDoneGateReceipt]:
        return [r for r in self._receipts if r.ticket_id == ticket_id]


def _receipt(
    *,
    sha: str = SHA,
    item: str = ITEM,
    verdict: _Verdict = "VERIFIED",
    run_id: str = "r1",
) -> ModelLocalDoneGateReceipt:
    return ModelLocalDoneGateReceipt(
        ticket_id=TICKET,
        contract_sha=sha,
        items=(ModelLocalDoneGateItemVerdict(item_id=item, verdict=verdict),),
        host="h053",
        run_id=run_id,
    )


def _status(receipts: list[ModelLocalDoneGateReceipt]) -> EnumEvidenceCheckStatus:
    raw: dict[str, Any] = {
        "ticket_id": TICKET,
        "dod_evidence": [
            {
                "id": ITEM,
                "description": "integration proof",
                "execution_scope": "local_done_gate",
                "checks": [{"check_type": "command", "check_value": "true"}],
            }
        ],
    }
    subject = ModelDodContractSubject(
        source=EnumDodContractSource.PRODUCT_REPOSITORY,
        repository="OmniNode-ai/omnimarket",
        commit_sha=SHA,
        repo_path=f"contracts/{TICKET}.yaml",
    )
    collector = EvidenceCollector(local_done_gate_receipts=_Source(receipts))
    results = collector._collect_impl(
        TICKET,
        None,
        EnumDodVerifyExecutionAudience.HOSTED,
        loaded_contract=(raw, subject),
    )
    (result,) = [r for r in results if r.evidence_id == ITEM]
    return result.status


def test_skipped_item_with_no_receipt_still_refuses() -> None:
    assert _status([]) is EnumEvidenceCheckStatus.SKIPPED


def test_matching_receipt_clears_the_skipped_item() -> None:
    assert _status([_receipt()]) is EnumEvidenceCheckStatus.VERIFIED


def test_receipt_for_another_revision_refuses() -> None:
    assert _status([_receipt(sha=OTHER_SHA)]) is EnumEvidenceCheckStatus.SKIPPED


def test_receipt_for_another_item_refuses() -> None:
    assert (
        _status([_receipt(item="some-other-item")]) is EnumEvidenceCheckStatus.SKIPPED
    )


def test_receipt_that_reads_failed_refuses() -> None:
    assert _status([_receipt(verdict="FAILED")]) is EnumEvidenceCheckStatus.SKIPPED


def test_later_failed_receipt_supersedes_earlier_verified() -> None:
    receipts = [_receipt(run_id="r1"), _receipt(verdict="FAILED", run_id="r2")]
    assert _status(receipts) is EnumEvidenceCheckStatus.SKIPPED


def test_filesystem_store_round_trips_and_ignores_malformed_lines(
    tmp_path: Path,
) -> None:
    store = FilesystemLocalDoneGateReceiptStore(tmp_path)
    store.append(_receipt())
    with (tmp_path / f"{TICKET}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    assert store.receipts_for(TICKET) == [_receipt()]
    assert store.receipts_for("OMN-1") == []


def test_receipt_rejects_a_short_contract_sha() -> None:
    with pytest.raises(ValueError, match="full lowercase hex"):
        _receipt(sha="abc123")


def test_handler_stores_a_receipt_carried_on_the_verdict(tmp_path: Path) -> None:
    from uuid import uuid4

    from omnimarket.models.model_dod_verify_state import ModelDodVerifyState
    from omnimarket.nodes.node_local_done_gate_receipt_store_effect.handlers.handler_local_done_gate_receipt_store import (
        HandlerLocalDoneGateReceiptStore,
    )

    store = FilesystemLocalDoneGateReceiptStore(tmp_path)
    handler = HandlerLocalDoneGateReceiptStore(store=store)
    bare = ModelDodVerifyState(
        correlation_id=uuid4(), ticket_id=TICKET, started_at=_NOW, completed_at=_NOW
    )
    assert handler.handle(bare) is False
    carrying = ModelDodVerifyState(
        correlation_id=uuid4(),
        ticket_id=TICKET,
        started_at=_NOW,
        completed_at=_NOW,
        local_done_gate_receipt=_receipt(),
    )
    assert handler.handle(carrying) is True
    assert store.receipts_for(TICKET) == [_receipt()]


def test_local_done_gate_run_publishes_receipt_and_hosted_run_does_not() -> None:
    from uuid import uuid4

    from omnimarket.models.model_dod_verify_state import ModelEvidenceCheckResult
    from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
        HandlerDodVerify,
    )
    from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
        ModelDodVerifyStartCommand,
    )

    subject = ModelDodContractSubject(
        source=EnumDodContractSource.PRODUCT_REPOSITORY,
        repository="OmniNode-ai/omnimarket",
        commit_sha=SHA,
        repo_path=f"contracts/{TICKET}.yaml",
    )

    class _Stub:
        contract_subject = subject
        acceptance_summary = None
        lookup_failure_cause = None
        lookup_failure_code = None

        def collect(self, **_: Any) -> list[ModelEvidenceCheckResult]:
            return [
                ModelEvidenceCheckResult(
                    evidence_id=ITEM,
                    description="integration proof",
                    status=EnumEvidenceCheckStatus.VERIFIED,
                )
            ]

    class _Handler(HandlerDodVerify):
        def _make_collector(self) -> Any:
            return _Stub()

    def run(audience: EnumDodVerifyExecutionAudience) -> Any:
        return _Handler()._handle_typed(
            ModelDodVerifyStartCommand(
                ticket_id=TICKET, correlation_id=uuid4(), execution_audience=audience
            )
        )

    receipt = run(
        EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE
    ).local_done_gate_receipt
    assert receipt is not None
    assert (receipt.ticket_id, receipt.contract_sha) == (TICKET, SHA)
    assert [(i.item_id, i.verdict) for i in receipt.items] == [(ITEM, "VERIFIED")]
    assert run(EnumDodVerifyExecutionAudience.HOSTED).local_done_gate_receipt is None


def test_receipt_store_node_contract_binds_the_handler_to_the_completed_topic() -> None:
    import yaml

    import omnimarket.nodes.node_local_done_gate_receipt_store_effect as node

    contract = yaml.safe_load(
        (Path(node.__file__).parent / "contract.yaml").read_text(encoding="utf-8")
    )
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.evt.omnimarket.dod-verify-completed.v1"
    ]
    routed = contract["handler_routing"]["handlers"][0]
    assert routed["handler"]["name"] == "HandlerLocalDoneGateReceiptStore"
    assert routed["event_model"]["name"] == "ModelDodVerifyCompletedEvent"
