# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-boundary gate-binding tests for node_occ_companion_compute (OMN-14406).

The seam this proves is PRODUCER -> GATE, not two independent unit suites
(OMN-14208): a receipt minted by :func:`compute_companion_plan` is fed through
the SAME function core's receipt-gate / occ-preflight recompute against
(``check_receipt_contract_binding`` +
``compute_contract_entry_sha256``), against the SAME rendered contract the
producer emitted.

PAIR_INCOMPATIBLE regression (OMN-14406): the producer previously minted the
per-entry ``contract_entry_sha256`` from a private ``_entry_sha256`` preimage
(``ticket|evidence|check_value|commit_sha``) that core can never recompute — it
folds ``commit_sha`` (absent from the contract) and omits the entry body. So
100% of minted receipts failed the gate with an "entry hash mismatch". These
tests drive that exact boundary so the mismatch is a RED test, not a latent
production hard-fail. Fixed: the producer now calls core's canonical
``compute_contract_entry_sha256`` over its own rendered contract.
"""

from __future__ import annotations

import hashlib

import pytest
import yaml
from omnibase_core.models.contracts.ticket.model_dod_receipt import ModelDodReceipt
from omnibase_core.validation.validator_receipt_gate import (
    check_receipt_contract_binding,
    compute_contract_entry_sha256,
)

from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_companion_compute.models.enum_companion_file_kind import (
    EnumCompanionFileKind,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_plan import (
    ModelCompanionFile,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
    ModelOccContractState,
)


def _probe(stdout: str = '{"number":321,"state":"OPEN"}') -> ModelObservedProbe:
    return ModelObservedProbe(
        command="gh pr view 321 --repo OmniNode-ai/omnimarket --json number,state",
        stdout=stdout,
        exit_code=0,
    )


def _request(**overrides: object) -> ModelOccCompanionRequest:
    base: dict[str, object] = {
        "repo": "OmniNode-ai/omnimarket",
        "pr_number": 321,
        "pr_head_sha": "b" * 40,
        "pr_title": "feat(OMN-9999): the thing",
        "pr_body": "Implements the thing.",
        "pr_state": "open",
        "pr_head_ref": "feature-branch",
        "run_timestamp": "2026-07-10T00:00:00Z",
        "product_probe": _probe(),
    }
    base.update(overrides)
    return ModelOccCompanionRequest.model_validate(base)


def _whole_file_hash(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _by_kind(
    files: tuple[ModelCompanionFile, ...], kind: EnumCompanionFileKind
) -> list[ModelCompanionFile]:
    return [f for f in files if f.kind == kind]


def _gate(
    receipt_file: ModelCompanionFile, contract_file: ModelCompanionFile
) -> str | None:
    """Drive one minted receipt through core's contract-binding gate.

    Recreates exactly what ``validate_pr_receipts`` /
    ``validate_occ_merge_eligibility`` do: parse the receipt into
    ``ModelDodReceipt``, parse the companion contract, and recompute the
    per-entry / whole-file binding.
    """
    receipt = ModelDodReceipt.model_validate(yaml.safe_load(receipt_file.content))
    contract_data = yaml.safe_load(contract_file.content)
    return check_receipt_contract_binding(
        receipt=receipt,
        contract_data=contract_data,
        evidence_item_id=receipt.evidence_item_id,
        whole_file_hash=_whole_file_hash(contract_file.content),
        is_bound_to_this_pr=True,
    )


@pytest.mark.unit
class TestFreshCompanionIsGateRecomputable:
    def test_downstream_receipt_binding_is_recomputable_by_core_gate(self) -> None:
        """RED before OMN-14406: the minted downstream receipt's
        ``contract_entry_sha256`` does not match core's recompute → the gate
        returns an "entry hash mismatch". GREEN after: identical, gate accepts.
        """
        plan = compute_companion_plan(_request())
        contract = _by_kind(plan.companion_files, EnumCompanionFileKind.CONTRACT)[0]
        downstream = _by_kind(
            plan.companion_files, EnumCompanionFileKind.DOWNSTREAM_RECEIPT
        )[0]

        result = _gate(downstream, contract)
        assert result is None, (
            "minted downstream receipt is NOT recomputable by core's "
            f"check_receipt_contract_binding: {result}"
        )

    def test_minted_entry_hash_equals_core_canonical_hash(self) -> None:
        """The value the producer wrote IS core's canonical per-entry hash —
        the strongest form of the seam match (byte-equal to the gate's own
        recompute), so a divergent private preimage cannot slip back in.
        """
        plan = compute_companion_plan(_request())
        contract = _by_kind(plan.companion_files, EnumCompanionFileKind.CONTRACT)[0]
        downstream = _by_kind(
            plan.companion_files, EnumCompanionFileKind.DOWNSTREAM_RECEIPT
        )[0]

        receipt = ModelDodReceipt.model_validate(yaml.safe_load(downstream.content))
        expected = compute_contract_entry_sha256(
            yaml.safe_load(contract.content), receipt.evidence_item_id
        )
        assert receipt.contract_entry_sha256 == expected


@pytest.mark.unit
class TestSelfBindReceiptIsDeclaredAndHashed:
    def test_self_bind_receipt_is_declared_and_carries_recomputable_hash(self) -> None:
        """OMN-14622: on pass 2 the self-bind item (``occ-self-bind-pr-N``) is a
        DECLARED ``dod_evidence`` entry in the companion contract — without it the
        OCC companion PR fails its OWN occ-preflight with ``pr_ticket_mismatch``
        (nothing binds the OCC PR). Because it is declared, its receipt carries a
        ``contract_entry_sha256`` that recomputes byte-equal to core's canonical
        per-entry hash. (Superseded the pre-14622 invariant that asserted the
        self-bind carried NO per-entry hash — that assumed the item was never
        declared, which was the very gap that forced the manual companion lane.)
        """
        plan = compute_companion_plan(
            _request(
                occ_pr_number=55,
                occ_head_sha="c" * 40,
                occ_probe=_probe('{"number":55,"state":"OPEN"}'),
            )
        )
        contract = _by_kind(plan.companion_files, EnumCompanionFileKind.CONTRACT)[0]
        parsed_contract = yaml.safe_load(contract.content)
        declared = {item["id"] for item in (parsed_contract.get("dod_evidence") or [])}
        self_binds = _by_kind(
            plan.companion_files, EnumCompanionFileKind.SELF_BIND_RECEIPT
        )
        assert self_binds, "expected a self-bind receipt when the OCC PR is known"
        for sb in self_binds:
            receipt = ModelDodReceipt.model_validate(yaml.safe_load(sb.content))
            assert receipt.evidence_item_id in declared, (
                "self-bind item must be a DECLARED dod_evidence entry so "
                "occ-preflight binds the OCC PR"
            )
            assert receipt.contract_entry_sha256 is not None
            expected = compute_contract_entry_sha256(
                parsed_contract, receipt.evidence_item_id
            )
            assert receipt.contract_entry_sha256 == expected
            # And the whole binding is gate-recomputable against the fresh contract.
            assert _gate(sb, contract) is None

    def test_every_declared_receipt_hashed_undeclared_not(self) -> None:
        """The full invariant, over EVERY minted receipt (mirrors the born-path
        emitter's guarantee): a receipt whose evidence_item_id is a declared
        contract item carries the per-entry hash; an undeclared one does not.
        """
        plan = compute_companion_plan(
            _request(
                occ_pr_number=55,
                occ_head_sha="c" * 40,
                occ_probe=_probe('{"number":55,"state":"OPEN"}'),
            )
        )
        contract = _by_kind(plan.companion_files, EnumCompanionFileKind.CONTRACT)[0]
        declared = {
            item["id"]
            for item in (yaml.safe_load(contract.content).get("dod_evidence") or [])
        }
        receipts = [
            f for f in plan.companion_files if f.kind != EnumCompanionFileKind.CONTRACT
        ]
        assert receipts
        for rf in receipts:
            receipt = ModelDodReceipt.model_validate(yaml.safe_load(rf.content))
            has_hash = receipt.contract_entry_sha256 is not None
            if receipt.evidence_item_id in declared:
                assert has_hash, (
                    f"{rf.path} binds DECLARED {receipt.evidence_item_id!r} but "
                    "carries no per-entry hash"
                )
                # And that hash is gate-recomputable against the fresh contract.
                assert _gate(rf, contract) is None
            else:
                assert not has_hash, (
                    f"{rf.path} binds UNDECLARED {receipt.evidence_item_id!r} but "
                    "carries a fabricated per-entry hash core cannot recompute"
                )


@pytest.mark.unit
class TestAppendStability:
    def test_entry_hash_survives_a_later_sibling_append(self) -> None:
        """The per-entry binding is append-stable (OMN-13888): appending a
        sibling ``dod_evidence`` entry to the contract leaves the original
        receipt's per-entry hash recomputable, whereas the whole-file hash rots.
        """
        plan = compute_companion_plan(_request())
        contract = _by_kind(plan.companion_files, EnumCompanionFileKind.CONTRACT)[0]
        downstream = _by_kind(
            plan.companion_files, EnumCompanionFileKind.DOWNSTREAM_RECEIPT
        )[0]
        receipt = ModelDodReceipt.model_validate(yaml.safe_load(downstream.content))
        assert receipt.contract_entry_sha256 is not None

        # Append an unrelated sibling entry (e.g. a later CI-outcome probe).
        contract_data = yaml.safe_load(contract.content)
        contract_data["dod_evidence"].append(
            {
                "id": "dod-OmniNode-ai-omnimarket-pr-321-ci",
                "description": "unrelated later-appended check",
                "source": "generated",
                "checks": [
                    {
                        "check_type": "command",
                        "check_value": "gh pr checks 321 --repo OmniNode-ai/omnimarket",
                    }
                ],
            }
        )
        appended_text = yaml.safe_dump(contract_data, sort_keys=False)

        # The per-entry hash of the ORIGINAL entry is unchanged by the append.
        recomputed = compute_contract_entry_sha256(
            yaml.safe_load(appended_text), receipt.evidence_item_id
        )
        assert receipt.contract_entry_sha256 == recomputed

        # The whole-file hash, by contrast, goes stale on the same append.
        assert receipt.contract_sha256 != _whole_file_hash(appended_text)


@pytest.mark.unit
class TestMergedTwoAudiencesSupersedeIsRecomputable:
    def _merged_state(self) -> ModelOccContractState:
        contract_text = (
            '---\nschema_version: "1.0.0"\nticket_id: "OMN-9999"\n'
            "dod_evidence:\n"
            "  - id: dod-omninode-ai-omnimarket-pr-100\n"
            "    description: prior consumer\n"
            "    source: generated\n"
            "    checks:\n"
            "      - check_type: command\n"
            '        check_value: "gh pr view 100 --repo OmniNode-ai/omnimarket --json number,state"\n'
        )
        return ModelOccContractState(
            ticket_id="OMN-9999",
            exists=True,
            merged=True,
            existing_entry_ids=("dod-omninode-ai-omnimarket-pr-100",),
            whole_file_sha256=hashlib.sha256(contract_text.encode()).hexdigest(),
            raw_contract_text=contract_text,
        )

    def test_supersede_receipt_recomputable_against_merged_contract(self) -> None:
        """A merged-contract 2nd consumer's net-new supersede receipt binds a
        prior entry that IS declared in the (frozen) merged contract, so its
        per-entry hash is recomputable against that merged contract.
        """
        state = self._merged_state()
        plan = compute_companion_plan(_request(occ_contract_states=(state,)))
        supersedes = _by_kind(
            plan.companion_files, EnumCompanionFileKind.SUPERSEDE_RECEIPT
        )
        assert supersedes, "merged 2nd consumer must emit supersede receipts"

        merged_contract = yaml.safe_load(state.raw_contract_text)
        for sf in supersedes:
            receipt = ModelDodReceipt.model_validate(yaml.safe_load(sf.content))
            assert receipt.contract_entry_sha256 is not None
            result = check_receipt_contract_binding(
                receipt=receipt,
                contract_data=merged_contract,
                evidence_item_id=receipt.evidence_item_id,
                whole_file_hash=_whole_file_hash(state.raw_contract_text),
                is_bound_to_this_pr=False,
            )
            assert result is None, f"supersede receipt not recomputable: {result}"
