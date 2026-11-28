# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A FAILED receipt from one host does not veto a VERIFIED reading from another (OMN-26729).

The fixture is the store file the dev runtime held for OMN-26115 on 2026-11-27: four
runs on lab-host-a that read every item VERIFIED, then one run on lab-host-b (a host
that cannot reach the lab) that read the integration-proof item FAILED, appended
last. The hosted verifier took that last line as the latest verdict and counted 10
of 11 items, so the ticket transition answered done_write_refused.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.models.model_local_done_gate_receipt import ModelLocalDoneGateReceipt
from omnimarket.models.model_local_done_gate_receipt_store import (
    receipt_reads_verified,
)

_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "omn26115_local_done_gate_receipts_recorded_2026_11_27.jsonl"
)
_TICKET = "OMN-26115"
_SHA = "872e7808f71194200b1dba4769c980661b2e8a78"
_ITEM = "dod-omn26115-integration-proof"


def _recorded() -> list[ModelLocalDoneGateReceipt]:
    return [
        ModelLocalDoneGateReceipt.model_validate_json(line)
        for line in _FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_recorded_store_ends_with_a_failed_reading_from_another_host() -> None:
    receipts = _recorded()
    assert receipts[-1].host == "lab-host-b"
    assert {i.verdict for i in receipts[-1].items if i.item_id == _ITEM} == {"FAILED"}


def test_a_verified_reading_stands_against_another_hosts_later_failure() -> None:
    receipt = receipt_reads_verified(
        _recorded(), ticket_id=_TICKET, contract_sha=_SHA, item_id=_ITEM
    )
    assert receipt is not None
    assert receipt.host == "lab-host-a"


def test_a_later_failure_from_the_same_host_still_supersedes() -> None:
    verified = [r for r in _recorded() if r.host == "lab-host-a"][-1]
    failed = ModelLocalDoneGateReceipt(
        ticket_id=_TICKET,
        contract_sha=_SHA,
        items=tuple(
            i.model_copy(update={"verdict": "FAILED"}) if i.item_id == _ITEM else i
            for i in verified.items
        ),
        host=verified.host,
        run_id="later-same-host",
    )
    assert (
        receipt_reads_verified(
            [*_recorded()[:-1], failed],
            ticket_id=_TICKET,
            contract_sha=_SHA,
            item_id=_ITEM,
        )
        is None
    )


def test_only_failed_readings_never_clear_the_item() -> None:
    failed_only = [r for r in _recorded() if r.host == "lab-host-b"]
    assert (
        receipt_reads_verified(
            failed_only, ticket_id=_TICKET, contract_sha=_SHA, item_id=_ITEM
        )
        is None
    )
