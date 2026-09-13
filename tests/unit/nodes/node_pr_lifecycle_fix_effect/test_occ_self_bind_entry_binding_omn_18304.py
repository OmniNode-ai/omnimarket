# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One OCC self-bind producer, per-entry bound (OMN-18304).

RED-first reproduction of the friction measured twice on 2026-09-13.

**The defect.** Two producers minted the same logical artifact with two
different bindings. The born path (``occ_companion_emitter``) wrote its
self-bind receipt undeclared, carrying only the whole-file ``contract_sha256``
— live on ``onex_change_control#9316``, whose receipt has no
``contract_entry_sha256`` at all. The compute path
(``node_occ_companion_compute``) declares the same id as a ``dod_evidence``
item and binds it per entry — live on ``onex_change_control#9327``.

**Why the whole-file pin is stale by construction.** ``contract_sha256`` digests
the WHOLE contract file, so any later append by any lane invalidates it. With
three companions on one ticket (``OMN-17341`` → OCC#9316, #9320, #9321, all
authoring ``contracts/OMN-17341.yaml``) the first companion's pin is dead the
moment the second appends, and rebinding is a race against the next merge. The
per-entry hash digests one ``dod_evidence`` entry plus the two header fields,
so an append to a sibling entry cannot touch it — that is the whole reason
``contract_entry_sha256`` exists (OMN-13888).

The tests below are the falsifier, not a description: `test_two_companions_...`
is RED against a whole-file-only self-bind and GREEN against a per-entry one,
with `test_whole_file_pin_is_the_thing_that_moves` as the positive control
proving the fixture really does restale a whole-file pin.
"""

from __future__ import annotations

import pytest
import yaml
from omnibase_core.validation.validator_receipt_gate import (
    compute_contract_entry_sha256,
)

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    compute_contract_sha256,
    rebind_contract_entry_sha256_in_text,
    rebind_contract_sha256_in_text,
    render_self_bind_dod_evidence_item,
    render_self_bind_receipt,
)

pytestmark = pytest.mark.unit

_TICKET = "OMN-17341"


def _self_bind_receipt(*, occ_pr_number: int) -> str:
    """Render the born-path self-bind receipt exactly as the emitter does."""
    return render_self_bind_receipt(
        ticket_id=_TICKET,
        evidence_id=f"occ-self-bind-pr-{occ_pr_number}",
        occ_pr_number=occ_pr_number,
        occ_repo="OmniNode-ai/onex_change_control",
        run_timestamp="2026-09-13T07:19:20Z",
        occ_commit_sha="34cc3dd847cec1af38f0537c44b2e5b20bb2c111",
        branch="auto/omninode-ai-omninode_infra-pr-1411-occ-autobind",
        probe_command=(
            "gh api repos/OmniNode-ai/onex_change_control/pulls/"
            f"{occ_pr_number}/files --paginate --jq '.[].sha'"
        ),
        probe_stdout="ad1e02e524c40b39abb0de9a3982b84b57c9ffff",
        exit_code=0,
    )


def _contract_with_self_binds(*occ_pr_numbers: int) -> str:
    """A companion contract declaring one self-bind item per OCC PR.

    Mirrors the live shape of ``contracts/OMN-17341.yaml``: one ticket, several
    companions, each appending its own rows to the same file.
    """
    head = (
        "---\n"
        'schema_version: "1.0.0"\n'
        f'ticket_id: "{_TICKET}"\n'
        'title: "Autobind OCC evidence for OMN-17341"\n'
        "dod_evidence:\n"
    )
    items = "".join(
        render_self_bind_dod_evidence_item(
            evidence_id=f"occ-self-bind-pr-{number}",
            occ_pr_number=number,
            occ_repo="OmniNode-ai/onex_change_control",
            ticket_id=_TICKET,
        )
        for number in occ_pr_numbers
    )
    return head + items


def _bind(receipt_text: str, contract_text: str, evidence_id: str) -> str:
    """Bind a receipt to a contract the way ``_rebind_receipts`` does."""
    bound = rebind_contract_sha256_in_text(
        receipt_text, compute_contract_sha256(contract_text.encode("utf-8"))
    )
    return rebind_contract_entry_sha256_in_text(
        bound,
        compute_contract_entry_sha256(yaml.safe_load(contract_text), evidence_id),
    )


def test_whole_file_pin_is_the_thing_that_moves() -> None:
    """Positive control: a second companion's append DOES restale a whole-file pin.

    Without this the RED test below could pass for the trivial reason that the
    fixture never changes the contract at all.
    """
    first = _contract_with_self_binds(9316)
    second = _contract_with_self_binds(9316, 9320)

    assert compute_contract_sha256(first.encode("utf-8")) != compute_contract_sha256(
        second.encode("utf-8")
    )
    # ...while the FIRST companion's own entry is byte-identical across both,
    # which is why a per-entry hash survives the append.
    assert compute_contract_entry_sha256(
        yaml.safe_load(first), "occ-self-bind-pr-9316"
    ) == compute_contract_entry_sha256(yaml.safe_load(second), "occ-self-bind-pr-9316")


def test_self_bind_receipt_declares_a_per_entry_hash() -> None:
    """The born-path self-bind renderer emits a ``contract_entry_sha256`` slot.

    RED before OMN-18304: ``render_self_bind_receipt`` emitted only
    ``contract_sha256``, so ``rebind_contract_entry_sha256_in_text`` — a
    substitution over an existing line — had nothing to write into and the
    receipt shipped whole-file-only. Live instance:
    ``drift/occ_bindings/OMN-17341/occ-self-bind-pr-9316/command.yaml``.
    """
    receipt = yaml.safe_load(_self_bind_receipt(occ_pr_number=9316))

    assert "contract_entry_sha256" in receipt, (
        "the self-bind receipt must carry a per-entry binding slot; a "
        "whole-file-only pin is invalidated by every later append to the "
        "contract by any lane (OMN-13888)"
    )


def test_two_companions_appending_in_sequence_keep_the_first_binding_valid() -> None:
    """Two companions, one contract: the first's binding must survive the second.

    This is the OMN-17341 shape (OCC#9316 then OCC#9320, both authoring
    ``contracts/OMN-17341.yaml``) reduced to its mechanism.
    """
    first_contract = _contract_with_self_binds(9316)
    first_receipt = _bind(
        _self_bind_receipt(occ_pr_number=9316), first_contract, "occ-self-bind-pr-9316"
    )

    # The second companion appends its own rows to the same contract.
    second_contract = _contract_with_self_binds(9316, 9320)

    parsed = yaml.safe_load(first_receipt)
    assert parsed["contract_entry_sha256"] == compute_contract_entry_sha256(
        yaml.safe_load(second_contract), "occ-self-bind-pr-9316"
    ), (
        "the first companion's self-bind binding went stale when the second "
        "companion appended to the same contract — the exact race the lane "
        "paid for on OMN-17341 (OCC#9316 / #9320 / #9321)"
    )
    # And the per-entry hash is real, not the unrebound PENDING sentinel.
    assert parsed["contract_entry_sha256"].startswith("sha256:")
    assert "PENDING" not in parsed["contract_entry_sha256"]


def test_declared_item_and_receipt_agree_on_the_evidence_id() -> None:
    """The declared ``dod_evidence`` id and the receipt's id are the same string.

    Without this the per-entry hash resolves against nothing and the rebinder
    silently leaves the PENDING sentinel in place.
    """
    item = yaml.safe_load(
        "dod_evidence:\n"
        + render_self_bind_dod_evidence_item(
            evidence_id="occ-self-bind-pr-9316",
            occ_pr_number=9316,
            occ_repo="OmniNode-ai/onex_change_control",
            ticket_id=_TICKET,
        )
    )["dod_evidence"][0]
    receipt = yaml.safe_load(_self_bind_receipt(occ_pr_number=9316))

    assert item["id"] == receipt["evidence_item_id"]
    assert item["checks"][0]["check_type"] == receipt["check_type"]
