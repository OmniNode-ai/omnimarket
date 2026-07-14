# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-boundary regression: the compute plan the RSD-3 write-EFFECT materializes
must pass the REAL occ-preflight validator (``validate_occ_merge_eligibility``) on
BOTH audiences — the product PR AND the OCC companion PR itself (OMN-14622).

This drives the actual seam (compute plan -> materialized files -> the real core
validator), not a hand-authored fixture. Before the OMN-14622 fix the OCC-PR
audience failed with ``pr_ticket_mismatch`` because the self-bind was a receipt
but not a declared contract entry; the RED control below reproduces exactly that
EXISTS-but-WRONG failure to prove the self-bind entry is load-bearing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.models.validation.model_occ_eligibility_input import (
    ModelOccEligibilityInput,
)
from omnibase_core.validation.validator_occ_merge_eligibility import (
    validate_occ_merge_eligibility,
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
)

_REPO = "OmniNode-ai/omnimarket"
_PRODUCT_PR = 1760
_PRODUCT_HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
_OCC_PR = 9999
_OCC_HEAD = "0fedcba987654321fedcba9876543210fedcba98"
_TICKET = "OMN-14608"


def _pass2_plan() -> ModelOccCompanionPlan:
    request = ModelOccCompanionRequest(
        repo=_REPO,
        pr_number=_PRODUCT_PR,
        pr_head_sha=_PRODUCT_HEAD,
        pr_title=f"feat({_TICKET}): thing",
        pr_body=f"Closes {_TICKET}",
        run_timestamp="2026-07-14T12:00:00Z",
        product_probe=ModelObservedProbe(
            command="gh pr view 1760",
            stdout='{"number":1760,"state":"OPEN"}',
            exit_code=0,
        ),
        occ_pr_number=_OCC_PR,
        occ_head_sha=_OCC_HEAD,
        occ_probe=ModelObservedProbe(
            command="gh pr view 9999",
            stdout='{"number":9999,"state":"OPEN"}',
            exit_code=0,
        ),
    )
    return compute_companion_plan(request)


def _materialize(plan: ModelOccCompanionPlan, root: Path) -> None:
    for f in plan.companion_files:
        path = root / f.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f.content, encoding="utf-8")


def _occ_pr_audience(root: Path) -> ModelOccEligibilityInput:
    return ModelOccEligibilityInput(
        repo="OmniNode-ai/onex_change_control",
        pr_number=_OCC_PR,
        pr_title=f"evidence({_TICKET}): OCC companion for omnimarket#{_PRODUCT_PR}",
        pr_body=f"Companion for {_TICKET}",
        pr_branch=f"auto/omninode-ai-omnimarket-pr-{_PRODUCT_PR}-occ-autobind",
        pr_commit_shas=(_OCC_HEAD,),
        pr_commit_texts=(f"evidence({_TICKET})",),
        occ_commit_sha=_OCC_HEAD,
        contracts_dir=root / "contracts",
        receipts_dir=root / "drift" / "dod_receipts",
    )


@pytest.mark.unit
def test_compute_plan_passes_product_pr_occ_preflight(tmp_path: Path) -> None:
    plan = _pass2_plan()
    _materialize(plan, tmp_path)
    snapshot = ModelOccEligibilityInput(
        repo=_REPO,
        pr_number=_PRODUCT_PR,
        pr_title=f"feat({_TICKET}): thing",
        pr_body=f"Closes {_TICKET}\nEvidence-Source: OCC#{_OCC_PR}\nEvidence-Ticket: {_TICKET}",
        pr_branch=f"jonah/{_TICKET.lower()}-thing",
        pr_commit_shas=(_PRODUCT_HEAD,),
        pr_commit_texts=(f"feat({_TICKET})",),
        occ_commit_sha=_OCC_HEAD,
        contracts_dir=tmp_path / "contracts",
        receipts_dir=tmp_path / "drift" / "dod_receipts",
    )
    result = validate_occ_merge_eligibility(snapshot)
    assert result.eligible, result.detail


@pytest.mark.unit
def test_compute_plan_passes_occ_companion_pr_own_occ_preflight(tmp_path: Path) -> None:
    """The audience that was RED before OMN-14622 — the OCC companion PR itself."""
    plan = _pass2_plan()
    _materialize(plan, tmp_path)
    result = validate_occ_merge_eligibility(_occ_pr_audience(tmp_path))
    assert result.eligible, result.detail


@pytest.mark.unit
def test_red_control_without_self_bind_entry_occ_pr_audience_fails(
    tmp_path: Path,
) -> None:
    """RED control (feedback_prove_red_against_exists_but_wrong): strip the
    self-bind ENTRY back out of the contract (and drop the self-bind receipt) —
    the EXISTS-but-WRONG pre-14622 shape — and the OCC-PR audience must go RED
    with pr_ticket_mismatch. Proves the self-bind entry is what makes it pass,
    not some unrelated artifact.
    """
    plan = _pass2_plan()
    _materialize(plan, tmp_path)

    # Revert to the buggy shape: remove the self-bind receipt file AND its
    # declared dod_evidence entry from the contract.
    self_bind = f"occ-self-bind-pr-{_OCC_PR}"
    (
        tmp_path / "drift" / "dod_receipts" / _TICKET / self_bind / "command.yaml"
    ).unlink()
    contract_path = tmp_path / "contracts" / f"{_TICKET}.yaml"
    lines = contract_path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept: list[str] = []
    skipping = False
    for line in lines:
        if line.startswith(f'  - id: "{self_bind}"'):
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
