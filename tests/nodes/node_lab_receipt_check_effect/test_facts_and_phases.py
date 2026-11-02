# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21826: what the node gathers (Q1 and Q3 of the design note) and how the phases differ.

* Q1: an unclassified file reaches the validator, so it counts and needs a receipt.
* Q3: a receipt of an older head is passed on and named as a head mismatch, never lost.
* The present phase never touches the lab; a verify run with an unreachable lab is refused.
* Anything unreadable blocks the merge: a moved head, GitHub, the carrier, a cut-short file list.
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_lab_receipt_phase import EnumLabReceiptPhase
from omnibase_core.models.lab_receipt.model_lab_receipt_changed_file import (
    ModelLabReceiptChangedFile,
)
from omnibase_core.models.lab_receipt.model_lab_receipt_document import (
    ModelLabReceiptDocument,
)

from omnimarket.nodes.node_lab_receipt_check_effect.handlers.handler_lab_receipt_check import (
    HandlerLabReceiptCheck,
)
from omnimarket.nodes.node_lab_receipt_check_effect.models import (
    EnumLabReceiptVerdict,
    ModelLabReceiptCheckResult,
)
from omnimarket.nodes.node_lab_receipt_check_effect.protocols import (
    ProtocolLabReceiptEventPublisher,
)
from tests.nodes.node_lab_receipt_check_effect.support import (
    InMemoryCarrier,
    InMemoryLab,
    RecordedGithub,
    load_document,
    make_request,
)

pytestmark = pytest.mark.unit

PRESENT = EnumLabReceiptPhase.PRESENT
VERIFY = EnumLabReceiptPhase.VERIFY
COMPLETED_TOPIC = "onex.evt.omnimarket.lab-receipt-check-completed.v1"


def _handler(
    document: ModelLabReceiptDocument,
    *,
    transport: RecordedGithub | None = None,
    carrier: InMemoryCarrier | None = None,
    lab_reader: InMemoryLab | None = None,
    event_bus: ProtocolLabReceiptEventPublisher | None = None,
) -> HandlerLabReceiptCheck:
    facts = document.facts
    return HandlerLabReceiptCheck(
        transport=transport or RecordedGithub(document),
        carrier=carrier or InMemoryCarrier(facts.receipts),
        lab_reader=lab_reader or InMemoryLab(facts.lab_reads),
        event_bus=event_bus,
    )


async def _check(
    handler: HandlerLabReceiptCheck,
    document: ModelLabReceiptDocument,
    phase: EnumLabReceiptPhase | None = None,
) -> ModelLabReceiptCheckResult:
    return await handler.handle(make_request(document, phase))


async def test_unclassified_and_head_move_an_unclassified_file_counts() -> None:
    document = load_document("counted_unclassified")
    result = await _check(_handler(document), document)
    assert result.required is True
    assert result.classes == ("unclassified",)


async def test_unclassified_and_head_move_no_receipt_for_an_unclassified_file_is_refused() -> (
    None
):
    document = load_document("counted_unclassified")
    stripped = document.model_copy(
        update={"facts": document.facts.model_copy(update={"receipts": ()})}
    )
    result = await _check(_handler(stripped), stripped)
    assert [i.code for i in result.issues] == ["receipt_required"]
    assert result.merge_blocked is True


async def test_unclassified_and_head_move_an_older_heads_receipt_is_named_not_lost() -> (
    None
):
    document = load_document("violation_receipt_head_mismatch")
    carrier = InMemoryCarrier(document.facts.receipts)
    result = await _check(_handler(document, carrier=carrier), document)
    assert carrier.reads == 1
    assert [i.code for i in result.issues] == ["receipt_head_mismatch"]
    assert "9a8b7c6d5e4f30211a2b3c4d5e6f708192a3b4c5" in result.issues[0].message
    assert result.merge_blocked is True


async def test_unclassified_and_head_move_a_pr_that_moved_past_the_request_is_unreadable() -> (
    None
):
    document = load_document("clean_receipt_required")
    moved = RecordedGithub(document, pr_head="f" * 40)
    result = await _check(_handler(document, transport=moved), document)
    assert result.verdict is EnumLabReceiptVerdict.UNREADABLE
    assert result.merge_blocked is True
    assert "not the one the caller will merge" in result.reason


async def test_phase_present_never_calls_the_lab_reader() -> None:
    document = load_document("violation_receipt_not_on_lab")
    lab = InMemoryLab(document.facts.lab_reads)
    result = await _check(_handler(document, lab_reader=lab), document, PRESENT)
    assert lab.asked == []
    assert result.issues == ()
    assert result.merge_blocked is False


async def test_phase_present_needs_no_lab_reader_at_all() -> None:
    document = load_document("clean_receipt_not_on_lab")
    handler = HandlerLabReceiptCheck(
        transport=RecordedGithub(document),
        carrier=InMemoryCarrier(document.facts.receipts),
    )
    result = await _check(handler, document, PRESENT)
    assert result.merge_blocked is False


async def test_phase_verify_asks_the_lab_about_each_receipt_host() -> None:
    document = load_document("clean_receipt_not_on_lab")
    lab = InMemoryLab(document.facts.lab_reads)
    result = await _check(_handler(document, lab_reader=lab), document, VERIFY)
    hosts = tuple(sorted({r.host for r in document.facts.receipts if r.host}))
    assert lab.asked == [hosts]
    assert result.merge_blocked is False


async def test_phase_verify_with_an_unreachable_lab_is_refused_never_passed() -> None:
    document = load_document("clean_receipt_not_on_lab")
    result = await _check(
        _handler(document, lab_reader=InMemoryLab(None)), document, VERIFY
    )
    assert [i.code for i in result.issues] == ["lab_read_unavailable"]
    assert result.verdict is EnumLabReceiptVerdict.REFUSED
    assert result.merge_blocked is True
    assert "the lab could not be read" in result.reason


async def test_phase_verify_with_no_lab_reader_configured_is_refused_never_passed() -> (
    None
):
    document = load_document("clean_receipt_not_on_lab")
    handler = HandlerLabReceiptCheck(
        transport=RecordedGithub(document),
        carrier=InMemoryCarrier(document.facts.receipts),
    )
    result = await _check(handler, document, VERIFY)
    assert [i.code for i in result.issues] == ["lab_read_unavailable"]
    assert result.merge_blocked is True


async def test_phase_verify_of_a_change_that_does_not_run_needs_no_receipt() -> None:
    document = load_document("exempt_docs_only")
    result = await _check(_handler(document), document, VERIFY)
    assert result.verdict is EnumLabReceiptVerdict.NOT_REQUIRED
    assert result.merge_blocked is False


async def test_unreadable_carrier_blocks() -> None:
    document = load_document("clean_receipt_required")
    handler = _handler(document, carrier=InMemoryCarrier((), fail=True))
    result = await _check(handler, document)
    assert result.verdict is EnumLabReceiptVerdict.UNREADABLE
    assert result.merge_blocked is True


async def test_no_carrier_configured_blocks() -> None:
    document = load_document("clean_receipt_required")
    handler = HandlerLabReceiptCheck(transport=RecordedGithub(document))
    result = await _check(handler, document)
    assert result.verdict is EnumLabReceiptVerdict.UNREADABLE
    assert "no receipt carrier is configured" in result.reason


@pytest.mark.parametrize("status", [403, 404, 500])
async def test_unreadable_file_list_blocks(status: int) -> None:
    document = load_document("clean_receipt_required")
    transport = RecordedGithub(document, files_status=status)
    result = await _check(_handler(document, transport=transport), document)
    assert result.verdict is EnumLabReceiptVerdict.UNREADABLE
    assert result.merge_blocked is True


async def test_unreadable_contract_blocks_but_a_missing_one_adds_no_criterion() -> None:
    document = load_document("clean_receipt_not_on_lab")
    broken = RecordedGithub(document, contract_status=500)
    result = await _check(_handler(document, transport=broken), document, PRESENT)
    assert result.verdict is EnumLabReceiptVerdict.UNREADABLE
    missing = RecordedGithub(document, contract_status=404)
    result = await _check(_handler(document, transport=missing), document, PRESENT)
    assert [i.code for i in result.issues] == ["receipt_criterion_missing"]


async def test_a_file_list_that_fills_every_page_is_unreadable_never_shorter() -> None:
    document = load_document("exempt_docs_only")
    # 30 pages of 100 are read; a list that fills them all may have been cut short.
    transport = RecordedGithub(document, extra_files=3000)
    result = await _check(_handler(document, transport=transport), document)
    assert result.verdict is EnumLabReceiptVerdict.UNREADABLE
    assert "was not read in full" in result.reason


async def test_a_long_file_list_is_read_page_by_page() -> None:
    document = load_document("exempt_docs_only")
    transport = RecordedGithub(document, extra_files=250)
    result = await _check(_handler(document, transport=transport), document)
    assert len([p for p in transport.requests if "/files" in p]) == 3
    assert result.verdict is EnumLabReceiptVerdict.NOT_REQUIRED


async def test_a_unit_behind_many_files_is_found_on_a_later_page() -> None:
    base = load_document("exempt_docs_only")
    unit = ModelLabReceiptChangedFile(path="launchd/example.plist", status="A")
    document = base.model_copy(
        update={
            "facts": base.facts.model_copy(
                update={"changed_files": (*base.facts.changed_files, unit)}
            )
        }
    )
    transport = RecordedGithub(document, extra_files=150)
    transport.entries.sort(key=lambda e: e["filename"] == unit.path)
    assert transport.entries[-1]["filename"] == unit.path
    result = await _check(_handler(document, transport=transport), document)
    assert result.required is True
    assert "host_unit" in result.classes


async def test_a_renamed_file_judges_the_old_path_as_removed() -> None:
    document = load_document("exempt_docs_only")
    transport = RecordedGithub(document)
    transport.entries = [
        {
            "filename": "docs/moved.md",
            "status": "renamed",
            "previous_filename": "launchd/example.plist",
        }
    ]
    result = await _check(_handler(document, transport=transport), document)
    assert result.required is True
    assert result.classes == ("host_unit",)


async def test_a_file_with_an_unknown_status_is_unreadable() -> None:
    document = load_document("exempt_docs_only")
    transport = RecordedGithub(document)
    transport.entries = [{"filename": "docs/a.md", "status": "teleported"}]
    result = await _check(_handler(document, transport=transport), document)
    assert result.verdict is EnumLabReceiptVerdict.UNREADABLE


class _Bus:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.sent: list[tuple[str, bytes | None, bytes]] = []

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        if self.fail:
            raise ConnectionError("bus down")
        self.sent.append((topic, key, value))


async def test_the_completed_event_is_published_and_a_failed_publish_changes_nothing() -> (
    None
):
    document = load_document("clean_receipt_required")
    good, bad = _Bus(fail=False), _Bus(fail=True)
    first = await _check(_handler(document, event_bus=good), document)
    second = await _check(_handler(document, event_bus=bad), document)
    assert [t for t, _, _ in good.sent] == [COMPLETED_TOPIC]
    assert first.verdict is second.verdict
