# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15485: the OCC companion producer must never rewrite a MERGED receipt.

Live defect this pins (onex_change_control#5599, companion for omnimarket#1973,
bot commit ``ca5e61cb2`` by ``node-occ-companion-effect`` 2026-07-30T12:23:55Z):
on the merged path ``compute_companion_plan`` emitted, for the SAME evidence item
in the SAME pass, BOTH

    drift/dod_receipts/<T>/dod-occ-evidence-admissibility-validator/command.supersede.1973.yaml   (correct, add)
    drift/dod_receipts/<T>/dod-occ-evidence-admissibility-validator/command.yaml                  (ILLEGAL, in-place rewrite)

because the R21b admissibility emission sat AFTER the merged/fresh if-else at
method-body indentation and therefore ran on both paths, missing the
``not (state.exists and state.merged)`` guard its deploy-assessment sibling
already carried. The required ``OCC Append-Only Gate`` rejects the second file as
``receipt_file_mutated``, so the companion is born hard-red and the product PR it
backs cannot land.

These tests drive the REAL producer entrypoint (``compute_companion_plan`` — the
same function ``node_occ_companion_effect`` calls, and the RSD-5 attestation
oracle) against a merged-receipt fixture built by the REAL contract renderer. No
surrogate, no monkeypatching (``feedback_test_the_artifact_that_runs``).

RED-before, verified against ``dev`` @ ``5a2e8bf5``:
  * ``test_merged_path_supersedes_admissibility_item_instead_of_rewriting_it``
    (both passes) — dev emits the illegal ``command.yaml`` alongside the
    supersede.
  * ``test_merged_path_refuses_to_rewrite_any_frozen_receipt_generic`` — dev
    silently emits the in-place rewrite through a DIFFERENT emission site (the
    downstream receipt), proving the point fix alone is not the mechanism.
Both are behavioural REDs against the shipped-but-wrong shape, not import errors
(``feedback_prove_red_against_exists_but_wrong``).
"""

from __future__ import annotations

import pytest
import yaml
from omnibase_core.models.contracts.ticket.model_receipt_supersession import (
    ModelReceiptSupersession,
)

from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    compute_companion_plan,
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
    ADMISSIBILITY_VALIDATOR_EVIDENCE_ID,
    compute_contract_sha256,
    render_compute_companion_contract,
)

# The live instance, verbatim: OCC#5599 / omnimarket#1973 / OMN-15483, whose
# merged contract was authored by the 1st consumer omnimarket#1972 under OCC#5596.
_TICKET = "OMN-15483"
_REPO = "OmniNode-ai/omnimarket"
_PRODUCT_PR = 1973
_PRODUCT_HEAD = "9a2361e5c1c2c3c4c5c6c7c8c9c0d1d2d3d4d5d6"
_OCC_PR = 5599
_OCC_HEAD = "e1e2e3e4e5e6e7e8e9e0f1f2f3f4f5f6f7f8f9f0"

_FIRST_PR = 1972
_FIRST_OCC_PR = 5596
_FIRST_ENTRY = f"dod-{_REPO.replace('/', '-')}-pr-{_FIRST_PR}"
_FIRST_SELF_BIND = f"occ-self-bind-pr-{_FIRST_OCC_PR}"
_OCC_REPO = "OmniNode-ai/onex_change_control"

_ADMISSIBILITY_DIR = (
    f"drift/dod_receipts/{_TICKET}/{ADMISSIBILITY_VALIDATOR_EVIDENCE_ID}"
)
_ADMISSIBILITY_FROZEN = f"{_ADMISSIBILITY_DIR}/command.yaml"
_ADMISSIBILITY_SUPERSEDE = f"{_ADMISSIBILITY_DIR}/command.supersede.{_PRODUCT_PR}.yaml"

_PRODUCT_PROBE = ModelObservedProbe(
    command=(
        f"gh api repos/{_REPO}/pulls/{_PRODUCT_PR}/files --jq '[.[].filename]|length'"
    ),
    stdout=f'{{"number":{_PRODUCT_PR},"state":"OPEN"}}',
    exit_code=0,
)
_OCC_PROBE = ModelObservedProbe(
    command=f"gh api repos/{_OCC_REPO}/pulls/{_OCC_PR}",
    stdout=f'{{"number":{_OCC_PR},"state":"OPEN"}}',
    exit_code=0,
)


def _merged_contract(first_entry: str = _FIRST_ENTRY) -> str:
    """The already-merged 1st-consumer contract, from the REAL renderer.

    ``render_compute_companion_contract`` ALWAYS declares the R21b admissibility
    item (OMN-15247), so every contract this producer has ever merged carries it
    — which is precisely why the merged path always collided. Rendered with the
    deploy + self-bind items too, so ``existing_entry_ids`` is the exact
    four-entry set OCC#5599 faced.
    """
    return render_compute_companion_contract(
        ticket_id=_TICKET,
        repo=_REPO,
        pr_number=_FIRST_PR,
        evidence_id=first_entry,
        self_bind_evidence_id=_FIRST_SELF_BIND,
        occ_pr_number=_FIRST_OCC_PR,
        occ_repo=_OCC_REPO,
        emit_deploy_assessment=True,
    )


def _declared_entry_ids(contract_text: str) -> tuple[str, ...]:
    """Parse the ids out of the merged contract (never hardcoded)."""
    parsed = yaml.safe_load(contract_text)
    return tuple(item["id"] for item in parsed["dod_evidence"])


def _merged_state(contract_text: str) -> ModelOccContractState:
    return ModelOccContractState(
        ticket_id=_TICKET,
        exists=True,
        merged=True,
        existing_entry_ids=_declared_entry_ids(contract_text),
        whole_file_sha256=compute_contract_sha256(contract_text),
        raw_contract_text=contract_text,
    )


def _request(
    *,
    contract_states: tuple[ModelOccContractState, ...],
    occ_pr_number: int | None,
) -> ModelOccCompanionRequest:
    return ModelOccCompanionRequest(
        repo=_REPO,
        pr_number=_PRODUCT_PR,
        pr_head_sha=_PRODUCT_HEAD,
        pr_title=f"fix({_TICKET}): 2nd consumer binds the merge hold check",
        pr_body=f"Closes {_TICKET}",
        run_timestamp="2026-07-30T12:23:55Z",
        product_probe=_PRODUCT_PROBE,
        occ_contract_states=contract_states,
        occ_pr_number=occ_pr_number,
        occ_head_sha=_OCC_HEAD if occ_pr_number is not None else None,
        occ_probe=_OCC_PROBE if occ_pr_number is not None else None,
    )


def _merged_plan(*, occ_pr_number: int | None) -> ModelOccCompanionPlan:
    return compute_companion_plan(
        _request(
            contract_states=(_merged_state(_merged_contract()),),
            occ_pr_number=occ_pr_number,
        )
    )


def _paths(plan: ModelOccCompanionPlan) -> set[str]:
    return {f.path for f in plan.companion_files}


# ---------------------------------------------------------------------------
# AC1 — the exact OCC#5599 shape, both authoring passes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("occ_pr_number", "label"),
    [(None, "pass 1 (no OCC PR yet — the ca5e61cb2 commit)"), (_OCC_PR, "pass 2")],
)
def test_merged_path_supersedes_admissibility_item_instead_of_rewriting_it(
    occ_pr_number: int | None, label: str
) -> None:
    """Merged path emits the supersede and NOT the in-place ``command.yaml``."""
    plan = _merged_plan(occ_pr_number=occ_pr_number)
    paths = _paths(plan)

    # Precondition: the fixture really is the colliding shape — the frozen
    # merged contract declares the admissibility item. Without this, the test
    # could pass vacuously against a contract that never had the item.
    assert ADMISSIBILITY_VALIDATOR_EVIDENCE_ID in _declared_entry_ids(
        _merged_contract()
    )

    assert _ADMISSIBILITY_SUPERSEDE in paths, (
        f"{label}: the rebind must be carried by a net-new supersession record"
    )
    assert _ADMISSIBILITY_FROZEN not in paths, (
        f"{label}: the producer emitted an in-place rewrite of the MERGED receipt "
        f"{_ADMISSIBILITY_FROZEN!r} — this is the OCC#5599 append-only violation "
        "(OMN-15485)"
    )


def test_merged_path_emits_no_plain_receipt_for_any_frozen_entry() -> None:
    """Generalised AC1: NO merged entry gets a plain ``command.yaml`` rewrite."""
    contract = _merged_contract()
    plan = _merged_plan(occ_pr_number=_OCC_PR)
    paths = _paths(plan)

    for entry in _declared_entry_ids(contract):
        frozen = f"drift/dod_receipts/{_TICKET}/{entry}/command.yaml"
        assert frozen not in paths, (
            f"merged entry {entry!r} was rewritten in place at {frozen!r}; "
            "corrections must be net-new .supersede.<NNNN>.yaml files"
        )
        assert (
            f"drift/dod_receipts/{_TICKET}/{entry}/command.supersede.{_PRODUCT_PR}.yaml"
            in paths
        ), f"merged entry {entry!r} lost its supersession rebind"


# ---------------------------------------------------------------------------
# AC5 — removing the in-place write loses no information.
# ---------------------------------------------------------------------------


def test_admissibility_supersede_carries_the_full_rebind() -> None:
    """The supersede carries every fact the in-place edit was writing."""
    plan = _merged_plan(occ_pr_number=_OCC_PR)
    supersede = next(
        f for f in plan.companion_files if f.path == _ADMISSIBILITY_SUPERSEDE
    )

    record = ModelReceiptSupersession.model_validate(yaml.safe_load(supersede.content))
    assert record.evidence_item_id == ADMISSIBILITY_VALIDATOR_EVIDENCE_ID
    assert record.supersedes == _ADMISSIBILITY_FROZEN
    assert record.tombstone is False

    replacement = record.replacement
    assert replacement is not None
    # The four rebind facts the illegal in-place edit was carrying.
    assert replacement.pr_number == _PRODUCT_PR
    assert replacement.commit_sha == _PRODUCT_HEAD
    assert (
        replacement.branch
        == f"auto/omninode-ai-omnimarket-pr-{_PRODUCT_PR}-occ-autobind"
    )
    assert replacement.probe_command == _PRODUCT_PROBE.command
    # The renderer normalises captured stdout to end in a newline (the literal
    # block-scalar round-trip, OMN-14714) — compare on the payload, not that.
    assert replacement.probe_stdout.rstrip("\n") == _PRODUCT_PROBE.stdout
    # Dual-hash binding the gate requires of a supersession replacement.
    assert supersede.contract_entry_sha256


# ---------------------------------------------------------------------------
# AC2 — the MECHANISM: a different emission site is refused too.
# ---------------------------------------------------------------------------


def test_merged_path_refuses_to_rewrite_any_frozen_receipt_generic() -> None:
    """A collision through the DOWNSTREAM receipt site is refused, loudly.

    The point fix guards ONE emission site. This drives a merged contract that
    already declares THIS PR's own downstream id — the second-order fragility the
    ticket names, where ``dod-<repo>-pr-<N>`` is safe only by naming coincidence
    — so the downstream receipt at the unguarded site targets a frozen merged
    path. The producer must refuse rather than emit.

    ``ValueError`` (not the subclass) is asserted so the RED against ``dev`` is
    BEHAVIOURAL — "DID NOT RAISE" — rather than a collection-time ImportError on
    a symbol dev does not have.
    """
    own_id = f"dod-{_REPO.replace('/', '-')}-pr-{_PRODUCT_PR}"
    contract = _merged_contract(first_entry=own_id)
    assert own_id in _declared_entry_ids(contract)

    # Broad ``ValueError`` (with the required PT011 ``match``) rather than the
    # subclass, ON PURPOSE — see the docstring: this keeps the dev-side RED a
    # behavioural "DID NOT RAISE" instead of a collection-time ImportError.
    with pytest.raises(ValueError, match=r"command\.supersede\.\d+\.yaml") as excinfo:
        compute_companion_plan(
            _request(
                contract_states=(_merged_state(contract),),
                occ_pr_number=None,
            )
        )

    message = str(excinfo.value)
    assert f"drift/dod_receipts/{_TICKET}/{own_id}/command.yaml" in message
    assert "command.supersede." in message


def test_append_only_invariant_is_a_typed_named_error() -> None:
    """The refusal is a typed error, not a bare ValueError or a silent drop."""
    from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
        AppendOnlyEmissionError,
    )

    own_id = f"dod-{_REPO.replace('/', '-')}-pr-{_PRODUCT_PR}"
    with pytest.raises(AppendOnlyEmissionError):
        compute_companion_plan(
            _request(
                contract_states=(_merged_state(_merged_contract(first_entry=own_id)),),
                occ_pr_number=None,
            )
        )


def test_invariant_falsifier_supersede_allowed_plain_refused() -> None:
    """Direct falsifier on the invariant: filename shape is what decides."""
    from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
        AppendOnlyEmissionError,
        assert_append_only_emissions,
    )
    from omnimarket.nodes.node_occ_companion_compute.models.enum_companion_file_kind import (
        EnumCompanionFileKind,
    )
    from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_plan import (
        ModelCompanionFile,
    )

    contract = _merged_contract()
    request = _request(contract_states=(_merged_state(contract),), occ_pr_number=None)

    def _file(path: str) -> ModelCompanionFile:
        return ModelCompanionFile(
            path=path,
            content="---\n",
            kind=EnumCompanionFileKind.DOWNSTREAM_RECEIPT,
            ticket_id=_TICKET,
        )

    # Allowed: the supersede add inside the same frozen directory.
    assert_append_only_emissions([_file(_ADMISSIBILITY_SUPERSEDE)], request)

    # Refused: a plain receipt at the frozen path.
    with pytest.raises(AppendOnlyEmissionError):
        assert_append_only_emissions([_file(_ADMISSIBILITY_FROZEN)], request)

    # Refused: any other non-supersede filename in a frozen entry directory —
    # the guard is directory-scoped, so a future emitter using a different
    # check_type cannot slip through on the filename.
    with pytest.raises(AppendOnlyEmissionError):
        assert_append_only_emissions(
            [_file(f"{_ADMISSIBILITY_DIR}/http.yaml")], request
        )

    # Untouched: a path under a NON-frozen entry is an add and stays legal.
    assert_append_only_emissions(
        [_file(f"drift/dod_receipts/{_TICKET}/dod-brand-new-item/command.yaml")],
        request,
    )


def test_invariant_refuses_contract_entry_edit_on_merged_path() -> None:
    """The contract half: merged bytes must remain a prefix (append-only)."""
    from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
        AppendOnlyEmissionError,
        assert_append_only_emissions,
    )
    from omnimarket.nodes.node_occ_companion_compute.models.enum_companion_file_kind import (
        EnumCompanionFileKind,
    )
    from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_plan import (
        ModelCompanionFile,
    )

    contract = _merged_contract()
    request = _request(contract_states=(_merged_state(contract),), occ_pr_number=None)
    edited = contract.replace(_FIRST_ENTRY, "dod-rewritten-entry", 1)

    with pytest.raises(AppendOnlyEmissionError):
        assert_append_only_emissions(
            [
                ModelCompanionFile(
                    path=f"contracts/{_TICKET}.yaml",
                    content=edited,
                    kind=EnumCompanionFileKind.CONTRACT,
                    ticket_id=_TICKET,
                )
            ],
            request,
        )


def test_merged_path_contract_is_append_only_by_construction() -> None:
    """Pass 2 appends the self-bind entry and preserves the merged bytes."""
    contract = _merged_contract()
    plan = _merged_plan(occ_pr_number=_OCC_PR)
    emitted = next(
        f for f in plan.companion_files if f.path == f"contracts/{_TICKET}.yaml"
    )
    assert emitted.content.startswith(contract)
    assert f"occ-self-bind-pr-{_OCC_PR}" in emitted.content


# ---------------------------------------------------------------------------
# AC3 — no regression of the fresh path (both directions tested).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "label"),
    [
        (None, "absent contract"),
        (
            ModelOccContractState(ticket_id=_TICKET, exists=True, merged=False),
            "exists-but-open contract",
        ),
    ],
)
def test_fresh_path_still_emits_the_admissibility_receipt(
    state: ModelOccContractState | None, label: str
) -> None:
    """R21b purpose preserved: the fresh path still mints the PASS receipt.

    The declared item without a PASS receipt is what makes
    ``validator_occ_merge_eligibility`` return ``MISSING_RECEIPT``, so suppressing
    this on the fresh path would trade one born-red class for another.
    """
    plan = compute_companion_plan(
        _request(
            contract_states=() if state is None else (state,),
            occ_pr_number=_OCC_PR,
        )
    )
    paths = _paths(plan)

    assert _ADMISSIBILITY_FROZEN in paths, (
        f"{label}: the fresh path must still emit the admissibility receipt"
    )
    assert _ADMISSIBILITY_SUPERSEDE not in paths, (
        f"{label}: nothing is frozen on the fresh path, so no supersede is due"
    )
    # And the contract that declares it is emitted alongside, so the item is
    # never declared without its receipt.
    assert f"contracts/{_TICKET}.yaml" in paths
