# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16120: a declared self-bind item must be minted with its receipt.

THE DEFECT. ``compute_companion_plan`` derives the self-bind DECLARATION from
one fact and the self-bind RECEIPT from two::

    self_bind_evidence_id = ... if request.occ_pr_number is not None else None
    if request.occ_pr_number is not None and request.occ_probe is not None: ...

So a pass-2 request carrying ``occ_pr_number`` without ``occ_probe`` emits a
contract that DECLARES ``occ-self-bind-pr-<N>`` and mints no receipt for it.
``validator_occ_merge_eligibility`` iterates the contract's ``dod_evidence`` and
resolves each item at ``drift/dod_receipts/<ticket>/<item>/<check_type>.yaml``,
so the companion is born INELIGIBLE on its own self-bind with
``reason=missing_receipt`` — the OMN-16120 class, and the last remaining way
this producer can mint one. Reproduced against dev tip (cb59d0e8): a probeless
pass-2 request returns a 3-file plan whose contract declares
``occ-self-bind-pr-7600`` and whose only receipts are the downstream and
admissibility ones.

WHY REFUSAL AND NOT SYNTHESIS. The receipt's ``probe_command`` /
``probe_stdout`` / ``exit_code`` are an OBSERVATION of the OCC PR, and
``compute_companion_plan`` is pure — it has no way to make one. Manufacturing a
plausible probe here would mint exactly the "surrogate that reads as proof"
OMN-16892 removed from the sibling producer. The two live callers
(``node_occ_companion_effect._observe_occ_probe`` and
``node_occ_attestation_observe._attest_companion``) both already observe the OCC
PR and both set ``occ_probe`` in the same ``model_copy`` that sets
``occ_pr_number``, each behind a total fallback — so failing closed refuses no
shape that exists today and makes the gap unshippable rather than silent.

WHY IT WAS NOT ALREADY COVERED. ``assert_receipt_keys_match_declarations``
(OMN-16859) asserts exactly this completeness invariant, and exempted this one
id by name as a KNOWN RESIDUAL. Its COMPLETENESS half is additionally scoped to
fresh/all-adds contracts, and the merged path declares the self-bind item too
(the plan appends it to the frozen 1st-consumer contract itself), so the merged
half of the defect was outside the invariant entirely.

Proof structure (feedback_prove_red_against_exists_but_wrong): the refusal legs
drive the REAL ``compute_companion_plan``, and the consumer leg drives
omnibase_core's REAL ``validate_occ_merge_eligibility`` over the producer's real
output on disk rather than restating this producer's convention back to itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_occ_eligibility_reason import EnumOccEligibilityReason
from omnibase_core.models.validation.model_occ_eligibility_input import (
    ModelOccEligibilityInput,
)
from omnibase_core.models.validation.model_occ_eligibility_result import (
    ModelOccEligibilityResult,
)
from omnibase_core.validation.validator_occ_merge_eligibility import (
    validate_occ_merge_eligibility,
)

from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    ReceiptKeyMismatchError,
    compute_companion_plan,
    declared_check_for,
    self_bind_evidence_id_for,
)
from omnimarket.nodes.node_occ_companion_compute.models.enum_companion_file_kind import (
    EnumCompanionFileKind,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_plan import (
    ModelOccCompanionPlan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
    ModelOccContractState,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    compute_contract_sha256,
    render_compute_companion_contract,
)

_TICKET = "OMN-16120"
_REPO = "OmniNode-ai/omnimarket"
_PR = 2204
_HEAD = "a1a2a3a4a5a6a7a8a9a0b1b2b3b4b5b6b7b8b9b0"
_SRC_FILE = "src/omnimarket/nodes/node_occ_companion_compute/handlers/x.py"

_OCC_REPO = "OmniNode-ai/onex_change_control"
_OCC_PR = 7600
_OCC_HEAD = "c1c2c3c4c5c6c7c8c9c0d1d2d3d4d5d6d7d8d9d0"

# The already-merged 1st consumer, whose contract this plan re-binds. The
# merged path appends the self-bind entry to THESE bytes, which is why the
# declaration is this plan's own and not the 1st consumer's frozen truth.
_FIRST_PR = 2203
_FIRST_ENTRY = f"dod-{_REPO.replace('/', '-')}-pr-{_FIRST_PR}"
_FIRST_OCC_PR = 7590


def _probe(pr: int, repo: str) -> ModelObservedProbe:
    return ModelObservedProbe(
        command=f"gh pr view {pr} --repo {repo} --json number,state",
        stdout=f'{{"number":{pr},"state":"OPEN"}}',
        exit_code=0,
    )


def _request(**overrides: object) -> ModelOccCompanionRequest:
    base: dict[str, object] = {
        "repo": _REPO,
        "pr_number": _PR,
        "pr_head_sha": _HEAD,
        "pr_title": f"fix({_TICKET}): the self-bind declaration is minted with its receipt",
        "pr_body": f"Closes {_TICKET}",
        "run_timestamp": "2026-08-29T00:00:00Z",
        "product_probe": _probe(_PR, _REPO),
        "changed_files": (_SRC_FILE,),
        "diff_total_lines": 40,
        "occ_repo": _OCC_REPO,
    }
    base.update(overrides)
    return ModelOccCompanionRequest(**base)  # type: ignore[arg-type]


def _pass_two(**overrides: object) -> ModelOccCompanionRequest:
    """A pass-2 request: the OCC PR is open and its number is known."""
    return _request(
        occ_pr_number=_OCC_PR,
        occ_head_sha=_OCC_HEAD,
        **overrides,
    )


def _merged_state() -> ModelOccContractState:
    contract_text = render_compute_companion_contract(
        ticket_id=_TICKET,
        repo=_REPO,
        pr_number=_FIRST_PR,
        evidence_id=_FIRST_ENTRY,
        self_bind_evidence_id=self_bind_evidence_id_for(_FIRST_OCC_PR),
        occ_pr_number=_FIRST_OCC_PR,
        occ_repo=_OCC_REPO,
    )
    parsed = yaml.safe_load(contract_text)
    return ModelOccContractState(
        ticket_id=_TICKET,
        exists=True,
        merged=True,
        existing_entry_ids=tuple(str(i["id"]) for i in parsed["dod_evidence"]),
        whole_file_sha256=compute_contract_sha256(contract_text),
        raw_contract_text=contract_text,
    )


def _contract_text(plan: ModelOccCompanionPlan) -> str:
    return next(
        f.content
        for f in plan.companion_files
        if f.kind is EnumCompanionFileKind.CONTRACT
    )


def _declared_ids(plan: ModelOccCompanionPlan) -> list[str]:
    parsed = yaml.safe_load(_contract_text(plan))
    return [str(item["id"]) for item in parsed["dod_evidence"]]


def _emitted_basenames(plan: ModelOccCompanionPlan) -> dict[str, set[str]]:
    """``{evidence item id -> {receipt basename, ...}}`` over every emission."""
    out: dict[str, set[str]] = {}
    for companion in plan.companion_files:
        if companion.kind is EnumCompanionFileKind.CONTRACT:
            continue
        parts = companion.path.split("/")
        out.setdefault(parts[-2], set()).add(parts[-1])
    return out


def _materialize(plan: ModelOccCompanionPlan, root: Path) -> None:
    for companion in plan.companion_files:
        target = root / companion.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(companion.content, encoding="utf-8")


def _eligibility(root: Path, *, occ_head_sha: str) -> ModelOccEligibilityResult:
    """Run omnibase_core's REAL eligibility validator over an OCC-shaped tree."""
    return validate_occ_merge_eligibility(
        ModelOccEligibilityInput(
            repo=_OCC_REPO,
            pr_number=_OCC_PR,
            pr_title=f"evidence({_TICKET}): OCC companion for {_REPO}#{_PR}",
            pr_body=f"Evidence companion for {_TICKET}.",
            pr_branch=f"auto/{_TICKET.lower()}-occ-autobind",
            pr_commit_shas=(occ_head_sha,),
            occ_commit_sha=occ_head_sha,
            contracts_dir=root / "contracts",
            receipts_dir=root / "drift" / "dod_receipts",
        )
    )


# ---------------------------------------------------------------------------
# AC1 — the mint refuses the declared-without-receipt shape, on BOTH paths.
# ---------------------------------------------------------------------------


def test_probeless_pass_two_mint_is_refused_on_the_fresh_path() -> None:
    """AC1. The OMN-16120 defect, stated as the producer-side refusal.

    RED against dev tip: the plan is returned, its contract declares
    ``occ-self-bind-pr-7600``, and no receipt is minted for it — a companion
    born INELIGIBLE with ``missing_receipt`` on its own self-bind, which is
    exactly the born-broken class this ticket names.
    """
    with pytest.raises(ReceiptKeyMismatchError) as excinfo:
        compute_companion_plan(_pass_two(occ_probe=None))

    message = str(excinfo.value)
    assert self_bind_evidence_id_for(_OCC_PR) in message
    # The message must name the CAUSE (the caller shape), not just the symptom,
    # so a lane reading it does not re-diagnose the generator from scratch the
    # way four lanes re-diagnosed OMN-16859 on 2026-08-28.
    assert "occ_probe" in message


def test_probeless_pass_two_mint_is_refused_on_the_merged_path() -> None:
    """AC1, second half — the half no invariant covered at all.

    ``assert_receipt_keys_match_declarations``' COMPLETENESS check is scoped to
    fresh/all-adds contracts, because on the merged path the 1st consumer's
    receipts are merged and immutable and this plan mints nothing at their base
    keys. The self-bind entry is the one item the merged path DOES declare
    itself (it appends the rendered entry to the frozen contract bytes), so it
    is this plan's own declaration and completeness genuinely holds for it.
    """
    with pytest.raises(ReceiptKeyMismatchError) as excinfo:
        compute_companion_plan(
            _pass_two(occ_probe=None, occ_contract_states=(_merged_state(),))
        )

    assert self_bind_evidence_id_for(_OCC_PR) in str(excinfo.value)


def test_the_refused_shape_is_missing_receipt_at_the_real_consumer(
    tmp_path: Path,
) -> None:
    """The JUSTIFICATION leg: why that shape must never be authored.

    Not a RED leg — it characterises omnibase_core, not this producer. It
    builds the exact artifact the probeless mint used to emit (contract with
    the self-bind entry declared, receipts for every OTHER item) and runs the
    REAL ``validate_occ_merge_eligibility`` over it, so the refusal above is
    anchored to consumer truth instead of to this producer's convention.
    """
    plan = compute_companion_plan(_pass_two(occ_probe=_probe(_OCC_PR, _OCC_REPO)))
    _materialize(plan, tmp_path)

    self_bind_id = self_bind_evidence_id_for(_OCC_PR)
    receipt_dir = tmp_path / "drift" / "dod_receipts" / _TICKET / self_bind_id
    assert receipt_dir.is_dir(), "precondition: the probeful mint writes it"
    # Reproduce the probeless shape by removing ONLY the self-bind receipt.
    for stale in receipt_dir.iterdir():
        stale.unlink()
    receipt_dir.rmdir()

    result = _eligibility(tmp_path, occ_head_sha=_OCC_HEAD)
    assert result.eligible is False
    assert result.reason is EnumOccEligibilityReason.MISSING_RECEIPT
    # The consumer names the exact key it could not resolve, in the
    # ``<ticket>:<item>:<check_type>`` form this producer's refusal quotes back.
    assert f"{_TICKET}:{self_bind_id}:command" in result.missing_or_nonpass_receipts


# ---------------------------------------------------------------------------
# AC2 — the honest mint is unchanged, on both paths.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "states",
    [
        pytest.param((), id="fresh"),
        pytest.param(None, id="merged"),
    ],
)
def test_probeful_pass_two_declares_and_mints_the_self_bind_receipt(
    states: tuple[ModelOccContractState, ...] | None,
) -> None:
    """AC2 CONTROL. A caller supplying both facts is byte-for-byte unaffected.

    Without this the refusal could pass AC1 by declining every pass-2 mint. The
    receipt key is derived through ``declared_check_for`` — the same authority
    the consumer uses — rather than restated as ``command.yaml``, so the pair
    cannot drift apart the way OMN-16859's did.
    """
    resolved = (_merged_state(),) if states is None else states
    plan = compute_companion_plan(
        _pass_two(occ_probe=_probe(_OCC_PR, _OCC_REPO), occ_contract_states=resolved)
    )

    self_bind_id = self_bind_evidence_id_for(_OCC_PR)
    assert self_bind_id in _declared_ids(plan)

    declared = declared_check_for(yaml.safe_load(_contract_text(plan)), self_bind_id)
    assert declared is not None
    assert f"{declared[0]}.yaml" in _emitted_basenames(plan)[self_bind_id]


def test_probeful_pass_two_plan_is_eligible_at_the_real_consumer(
    tmp_path: Path,
) -> None:
    """AC2, at the consumer. The honest mint clears eligibility with zero edits.

    This is the ticket's acceptance criterion executed rather than asserted:
    a fresh machine mint for a self-bind contract passes with no hand-authored
    entry anywhere in the tree.
    """
    plan = compute_companion_plan(_pass_two(occ_probe=_probe(_OCC_PR, _OCC_REPO)))
    _materialize(plan, tmp_path)

    result = _eligibility(tmp_path, occ_head_sha=_OCC_HEAD)
    # CodeRabbit (omnimarket#2203): asserting only "not MISSING_RECEIPT" would
    # pass while the companion is ineligible for some OTHER reason, which is not
    # the criterion. The full verdict holds — the plan is hash-bound and PR-bound
    # too — so assert the criterion itself and carry the reason for diagnosis.
    assert result.eligible is True, (
        f"machine mint still needs a hand edit: {result.reason} — {result.detail}"
    )
    assert result.reason is EnumOccEligibilityReason.ELIGIBLE


def test_pass_one_mint_declares_no_self_bind_and_is_not_refused() -> None:
    """CONTROL. Before the OCC PR exists there is nothing to declare or mint.

    Guards against a refusal that fires on the ``occ_pr_number is None`` pass-1
    request every companion starts from — which would take the whole producer
    offline rather than close one gap.
    """
    plan = compute_companion_plan(_request())
    assert not any(item.startswith("occ-self-bind-pr-") for item in _declared_ids(plan))
    assert not any(
        key.startswith("occ-self-bind-pr-") for key in _emitted_basenames(plan)
    )


def test_merged_path_still_mints_nothing_at_the_first_consumers_base_keys() -> None:
    """CONTROL. The carve-in is exactly one id — completeness did not widen.

    On the merged path the 1st consumer's items are already-merged, immutable
    receipts this plan re-binds with net-new supersessions and mints nothing at
    their base keys. Demanding an emission there would refuse every legitimate
    2nd-consumer companion, so this asserts the merged plan is still authored
    while carrying no plain ``command.yaml`` for the prior entry.
    """
    plan = compute_companion_plan(
        _pass_two(
            occ_probe=_probe(_OCC_PR, _OCC_REPO),
            occ_contract_states=(_merged_state(),),
        )
    )
    assert "command.yaml" not in _emitted_basenames(plan).get(_FIRST_ENTRY, set())
    assert any(
        name.startswith("command.supersede.")
        for name in _emitted_basenames(plan)[_FIRST_ENTRY]
    )


def test_the_self_bind_id_has_a_single_derivation() -> None:
    """The id the contract declares and the id the invariant checks are one call.

    Before this ticket the string ``f"occ-self-bind-pr-{...}"`` was rebuilt at
    three sites in this module (the declaration, the receipt, and the
    invariant's own exemption). Three derivations of one identifier is three
    things to drift; the emitted contract must agree with the helper.
    """
    plan = compute_companion_plan(_pass_two(occ_probe=_probe(_OCC_PR, _OCC_REPO)))
    assert self_bind_evidence_id_for(_OCC_PR) in _declared_ids(plan)
    assert self_bind_evidence_id_for(_OCC_PR) == f"occ-self-bind-pr-{_OCC_PR}"
