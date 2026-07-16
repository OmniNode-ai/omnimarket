# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-boundary regression: the MERGED-path (2nd-consumer) compute plan must
pass the REAL occ-preflight validator (``validate_occ_merge_eligibility``) on BOTH
audiences — the product PR AND the OCC companion PR itself (OMN-14623).

This is the merged-path twin of ``test_companion_plan_passes_occ_preflight.py``
(the OMN-14622 fresh-path test). It drives the actual seam (compute plan ->
materialized files layered over a simulated OCC-main merged contract -> the real
core validator), not a hand-authored fixture.

Two coupled defects the fix closes, each with a RED control that reproduces the
EXISTS-but-WRONG pre-14623 shape (feedback_prove_red_against_exists_but_wrong):

  * defect (b): the supersede file was rendered as a plain ``ModelDodReceipt``,
    but ``resolve_supersession`` parses ``command.supersede.<NNNN>.yaml`` STRICTLY
    as a ``ModelReceiptSupersession`` -> schema-reject -> ``nonpass_receipt``.
  * defect (a): the merged contract is frozen and did not declare the OCC
    self-bind entry, so the OCC companion PR's own occ-preflight found no PASS
    receipt bound to the OCC PR -> ``pr_ticket_mismatch``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.models.validation.model_occ_eligibility_input import (
    ModelOccEligibilityInput,
)
from omnibase_core.validation.validator_occ_merge_eligibility import (
    validate_occ_merge_eligibility,
)
from omnibase_core.validation.validator_receipt_gate import (
    compute_contract_entry_sha256,
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
    compute_contract_sha256,
    render_compute_companion_contract,
    render_compute_receipt,
)

# 2nd consumer (this product PR).
_REPO = "OmniNode-ai/omnibase_core"
_PRODUCT_PR = 4242
_PRODUCT_HEAD = "c1c2c3c4c5c6c7c8c9c0d1d2d3d4d5d6d7d8d9d0"
_OCC_PR = 8888
_OCC_HEAD = "e1e2e3e4e5e6e7e8e9e0f1f2f3f4f5f6f7f8f9f0"
_TICKET = "OMN-14623"

# 1st consumer — the already-merged OCC contract this ticket is a 2nd consumer of.
_FIRST_REPO = "OmniNode-ai/omnimarket"
_FIRST_PR = 1000
_FIRST_ENTRY = f"dod-{_FIRST_REPO.replace('/', '-')}-pr-{_FIRST_PR}"
_SELF_BIND = f"occ-self-bind-pr-{_OCC_PR}"


def _merged_contract() -> str:
    """A realistic already-merged contract authored by the 1st consumer."""
    return render_compute_companion_contract(
        ticket_id=_TICKET,
        repo=_FIRST_REPO,
        pr_number=_FIRST_PR,
        evidence_id=_FIRST_ENTRY,
    )


def _merged_pass2_plan() -> ModelOccCompanionPlan:
    merged = _merged_contract()
    request = ModelOccCompanionRequest(
        repo=_REPO,
        pr_number=_PRODUCT_PR,
        pr_head_sha=_PRODUCT_HEAD,
        pr_title=f"feat({_TICKET}): 2nd consumer",
        pr_body=f"Closes {_TICKET}",
        run_timestamp="2026-07-16T12:00:00Z",
        product_probe=ModelObservedProbe(
            command=f"gh pr view {_PRODUCT_PR}",
            stdout=f'{{"number":{_PRODUCT_PR},"state":"OPEN"}}',
            exit_code=0,
        ),
        occ_pr_number=_OCC_PR,
        occ_head_sha=_OCC_HEAD,
        occ_probe=ModelObservedProbe(
            command=f"gh pr view {_OCC_PR}",
            stdout=f'{{"number":{_OCC_PR},"state":"OPEN"}}',
            exit_code=0,
        ),
        occ_contract_states=(
            ModelOccContractState(
                ticket_id=_TICKET,
                exists=True,
                merged=True,
                existing_entry_ids=(_FIRST_ENTRY,),
                whole_file_sha256=compute_contract_sha256(merged),
                raw_contract_text=merged,
            ),
        ),
    )
    return compute_companion_plan(request)


def _write_occ_main(root: Path) -> None:
    """Materialize the OCC-main state the 2nd-consumer PR is layered over: the
    merged contract plus the 1st-consumer base receipt for the prior entry."""
    merged = _merged_contract()
    contract_path = root / "contracts" / f"{_TICKET}.yaml"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text(merged, encoding="utf-8")

    parsed = yaml.safe_load(merged)
    base_receipt = render_compute_receipt(
        ticket_id=_TICKET,
        evidence_id=_FIRST_ENTRY,
        check_value=f"gh pr view {_FIRST_PR} --repo {_FIRST_REPO} --json number,state",
        contract_sha256=compute_contract_sha256(merged),
        contract_entry_sha256=compute_contract_entry_sha256(parsed, _FIRST_ENTRY),
        run_timestamp="2026-07-01T00:00:00Z",
        commit_sha="a1a2a3a4a5a6a7a8a9a0b1b2b3b4b5b6b7b8b9b0",
        runner="node_occ_companion_compute",
        verifier="occ-evidence-source-autobind",
        probe_command=f"gh pr view {_FIRST_PR}",
        probe_stdout=f'{{"number":{_FIRST_PR},"state":"MERGED"}}',
        actual_output="PASS: 1st consumer",
        exit_code=0,
        pr_number=_FIRST_PR,
        branch="auto/first-consumer",
    )
    base_path = (
        root / "drift" / "dod_receipts" / _TICKET / _FIRST_ENTRY / "command.yaml"
    )
    base_path.parent.mkdir(parents=True, exist_ok=True)
    base_path.write_text(base_receipt, encoding="utf-8")


def _materialize(plan: ModelOccCompanionPlan, root: Path) -> None:
    """Layer the net-new OCC PR files over the simulated OCC-main state."""
    _write_occ_main(root)
    for f in plan.companion_files:
        path = root / f.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f.content, encoding="utf-8")


def _product_pr_audience(root: Path) -> ModelOccEligibilityInput:
    return ModelOccEligibilityInput(
        repo=_REPO,
        pr_number=_PRODUCT_PR,
        pr_title=f"feat({_TICKET}): 2nd consumer",
        pr_body=(
            f"Closes {_TICKET}\nEvidence-Source: OCC#{_OCC_PR}\n"
            f"Evidence-Ticket: {_TICKET}"
        ),
        pr_branch=f"jonah/{_TICKET.lower()}-second-consumer",
        pr_commit_shas=(_PRODUCT_HEAD,),
        pr_commit_texts=(f"feat({_TICKET})",),
        occ_commit_sha=_OCC_HEAD,
        contracts_dir=root / "contracts",
        receipts_dir=root / "drift" / "dod_receipts",
    )


def _occ_pr_audience(root: Path) -> ModelOccEligibilityInput:
    return ModelOccEligibilityInput(
        repo="OmniNode-ai/onex_change_control",
        pr_number=_OCC_PR,
        pr_title=f"evidence({_TICKET}): OCC companion for {_REPO}#{_PRODUCT_PR}",
        pr_body=f"Companion for {_TICKET}",
        pr_branch=f"auto/omninode-ai-omnibase-core-pr-{_PRODUCT_PR}-occ-autobind",
        pr_commit_shas=(_OCC_HEAD,),
        pr_commit_texts=(f"evidence({_TICKET})",),
        occ_commit_sha=_OCC_HEAD,
        contracts_dir=root / "contracts",
        receipts_dir=root / "drift" / "dod_receipts",
    )


@pytest.mark.unit
def test_merged_plan_passes_product_pr_occ_preflight(tmp_path: Path) -> None:
    plan = _merged_pass2_plan()
    _materialize(plan, tmp_path)
    result = validate_occ_merge_eligibility(_product_pr_audience(tmp_path))
    assert result.eligible, result.detail


@pytest.mark.unit
def test_merged_plan_passes_occ_companion_pr_own_occ_preflight(tmp_path: Path) -> None:
    """The OCC-PR audience — RED before OMN-14623 with pr_ticket_mismatch because
    the merged contract never declared the self-bind entry."""
    plan = _merged_pass2_plan()
    _materialize(plan, tmp_path)
    result = validate_occ_merge_eligibility(_occ_pr_audience(tmp_path))
    assert result.eligible, result.detail


@pytest.mark.unit
def test_merged_supersede_file_is_a_valid_receipt_supersession(tmp_path: Path) -> None:
    """The supersede file must parse as a ModelReceiptSupersession with a
    dual-hash replacement (both whole-file and per-entry), not a plain receipt."""
    from omnibase_core.models.contracts.ticket.model_receipt_supersession import (
        ModelReceiptSupersession,
    )

    plan = _merged_pass2_plan()
    supersede = next(f for f in plan.companion_files if ".supersede." in f.path)
    record = ModelReceiptSupersession.model_validate(yaml.safe_load(supersede.content))
    assert record.replacement is not None
    assert record.replacement.contract_entry_sha256 is not None
    assert record.replacement.contract_sha256 is not None
    assert record.replacement.pr_number == _PRODUCT_PR


@pytest.mark.unit
def test_red_control_plain_receipt_supersede_fails_schema(tmp_path: Path) -> None:
    """RED control for defect (b): revert the supersede to the pre-14623 plain
    ModelDodReceipt shape and the product-PR audience must go RED — the
    supersession record is schema-rejected -> nonpass_receipt."""
    plan = _merged_pass2_plan()
    _materialize(plan, tmp_path)

    # Overwrite each supersede with the buggy plain-receipt render.
    parsed = yaml.safe_load(_merged_contract())
    plain = render_compute_receipt(
        ticket_id=_TICKET,
        evidence_id=_FIRST_ENTRY,
        check_value=f"gh pr view {_PRODUCT_PR} --repo {_REPO} --json number,state",
        contract_sha256=compute_contract_sha256(_merged_contract()),
        contract_entry_sha256=compute_contract_entry_sha256(parsed, _FIRST_ENTRY),
        run_timestamp="2026-07-16T12:00:00Z",
        commit_sha=_PRODUCT_HEAD,
        runner="node_occ_companion_compute",
        verifier="occ-evidence-source-autobind",
        probe_command=f"gh pr view {_PRODUCT_PR}",
        probe_stdout=f'{{"number":{_PRODUCT_PR},"state":"OPEN"}}',
        actual_output="PASS: plain-receipt supersede (buggy pre-14623 shape)",
        exit_code=0,
        pr_number=_PRODUCT_PR,
        branch="auto/second-consumer",
    )
    supersede_path = (
        tmp_path
        / "drift"
        / "dod_receipts"
        / _TICKET
        / _FIRST_ENTRY
        / f"command.supersede.{_PRODUCT_PR}.yaml"
    )
    assert supersede_path.is_file(), "fixture must have produced a supersede file"
    supersede_path.write_text(plain, encoding="utf-8")

    result = validate_occ_merge_eligibility(_product_pr_audience(tmp_path))
    assert not result.eligible
    assert result.reason.value == "nonpass_receipt", result.detail


@pytest.mark.unit
def test_red_control_without_self_bind_entry_occ_pr_audience_fails(
    tmp_path: Path,
) -> None:
    """RED control for defect (a): strip the self-bind ENTRY back out of the
    (re-emitted) contract — the pre-14623 frozen-contract shape — and the OCC-PR
    audience must go RED with pr_ticket_mismatch, proving the appended self-bind
    entry is what makes the OCC PR bind."""
    plan = _merged_pass2_plan()
    _materialize(plan, tmp_path)

    contract_path = tmp_path / "contracts" / f"{_TICKET}.yaml"
    lines = contract_path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept: list[str] = []
    skipping = False
    for line in lines:
        if line.startswith(f'  - id: "{_SELF_BIND}"'):
            skipping = True
            continue
        if skipping and line.startswith("  - id:"):
            skipping = False
        if not skipping:
            kept.append(line)
    contract_path.write_text("".join(kept), encoding="utf-8")

    result = validate_occ_merge_eligibility(_occ_pr_audience(tmp_path))
    assert not result.eligible
    assert result.reason.value == "pr_ticket_mismatch", result.detail
