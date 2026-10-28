# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_pr_handoff_decision_compute: refuse a handoff for a repository the controller will not land.

The landing scope is a fact the caller reads from its ``landing_scope`` overlay
and passes in. The fixtures below stand in for two deployments' overlays, with
invented repository names: the node itself carries none.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.pr_handoff import (
    EnumPrHandoffErrorCode,
    EnumPrHandoffVerdict,
    ModelPrHandoffDecision,
    ModelPrHandoffDecisionRequest,
)
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_pr_handoff_decision_compute.handlers.handler_pr_handoff_decision import (
    HandlerPrHandoffDecision,
)
from omnimarket.nodes.node_pr_handoff_ledger_effect.handlers.handler_pr_handoff_ledger_effect import (
    stamp_rows,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from tests.chains.pr_handoff import _builders as b

pytestmark = pytest.mark.unit

CID = UUID("6f0c3a52-8d2b-4a51-9a50-55a1d3f2b0c4")
E = EnumPrHandoffErrorCode
REPO = "acme-widgets"
KEY = f"{REPO}#{b.PR}"

# Two deployments' ``landing_scope`` overlays, as the caller reads them.
OVERLAY_LANDING_THE_REPO: dict[str, Any] = {
    "landing_scope": {"repos": ["acme-widgets", "acme-gadgets"]}
}
OVERLAY_NOT_LANDING_THE_REPO: dict[str, Any] = {
    "landing_scope": {"repos": ["acme-gadgets"]}
}


def _scope(overlay: dict[str, Any]) -> tuple[str, ...]:
    return tuple(overlay["landing_scope"]["repos"])


def _decide(
    landing_scope: tuple[str, ...] | None = None,
    *,
    observed: bool = True,
    now_offset: int = 60,
) -> ModelPrHandoffDecision:
    scoped: dict[str, Any] = (
        {} if landing_scope is None else {"landing_scope": landing_scope}
    )
    return HandlerPrHandoffDecision().handle(
        ModelPrHandoffDecisionRequest(
            request=b.request(CID, repo=REPO, claim_prs=(), lab_proof=b.lab_comment()),
            observation=(
                b.observation(b.at(60), repo=REPO).observation() if observed else None
            ),
            now=b.at(now_offset),
            **scoped,
        )
    )


def test_out_of_scope_repository_is_refused_with_the_named_reason() -> None:
    decision = _decide(_scope(OVERLAY_NOT_LANDING_THE_REPO))
    assert decision.verdict is EnumPrHandoffVerdict.REFUSE
    assert decision.error_code is E.REPO_OUT_OF_LANDING_SCOPE
    assert E.REPO_OUT_OF_LANDING_SCOPE.value == "repo-out-of-landing-scope"
    assert decision.rows is None
    assert REPO in decision.detail


def test_the_same_request_is_refused_under_one_overlay_and_ready_under_another() -> (
    None
):
    refused = _decide(_scope(OVERLAY_NOT_LANDING_THE_REPO))
    ready = _decide(_scope(OVERLAY_LANDING_THE_REPO))
    assert refused.error_code is E.REPO_OUT_OF_LANDING_SCOPE
    assert ready.verdict is EnumPrHandoffVerdict.READY


def test_in_scope_decision_is_the_decision_of_an_absent_scope() -> None:
    assert _decide(_scope(OVERLAY_LANDING_THE_REPO)) == _decide(None)
    assert _decide(None).verdict is EnumPrHandoffVerdict.READY


def test_absent_scope_is_not_a_refusal_and_an_empty_scope_lands_nothing() -> None:
    assert _decide(None).error_code is None
    assert _decide(()).error_code is E.REPO_OUT_OF_LANDING_SCOPE


def test_scope_match_is_exact_not_a_prefix() -> None:
    assert _decide(("acme-widget",)).error_code is E.REPO_OUT_OF_LANDING_SCOPE
    assert _decide(("acme-widgets-extra",)).error_code is E.REPO_OUT_OF_LANDING_SCOPE


def test_out_of_scope_is_refused_without_waiting_for_an_observation() -> None:
    decision = _decide(_scope(OVERLAY_NOT_LANDING_THE_REPO), observed=False)
    assert decision.error_code is E.REPO_OUT_OF_LANDING_SCOPE
    assert _decide(None, observed=False).verdict is EnumPrHandoffVerdict.WAIT


def test_refusal_row_is_data_on_the_decision_and_the_decision_writes_nothing() -> None:
    decision = _decide(_scope(OVERLAY_NOT_LANDING_THE_REPO))
    stamp = b.stamp(b.at(0))
    assert decision.refusal_row == (
        f"{stamp} | STATUS | lane={b.LANE} | repo={REPO} | pr={KEY} | "
        f"head={b.HEAD[:7]} | handoff-refused={KEY} | "
        f"reason=repo-out-of-landing-scope | refused_to=landing-controller | "
        f"Handoff of {KEY} from {b.LANE} refused: {REPO} is outside the landing "
        "scope the caller supplied, so the controller will not land it. "
        f"Decided at {b.stamp(b.at(60))} by node_pr_handoff_decision_compute.\n"
    )
    assert _decide(_scope(OVERLAY_LANDING_THE_REPO)).refusal_row is None
    assert _decide(None).refusal_row is None


def test_refusal_row_is_appended_by_the_work_ledger_append_effect() -> None:
    decision = _decide(_scope(OVERLAY_NOT_LANDING_THE_REPO))
    assert decision.refusal_row is not None
    ledger = b.InMemoryLedgerFile()
    request_id = uuid4()
    receipt = HandlerWorkLedgerAppendEffect(
        runner=ledger,
        reader=ledger,
        host_name="h200",
        writer_principals=["scope-test"],
    ).handle(
        ModelWorkLedgerAppendRequest(
            request_id=request_id,
            rows=stamp_rows(decision.refusal_row, str(request_id), "h202"),
            requested_by_lane=b.LANE,
            requesting_host="h202",
            requested_at=b.at(61),
        ),
        writer_principal="scope-test",
    )
    assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
    assert f"handoff-refused={KEY}" in ledger.read_text()


def test_decision_shape_ties_the_refusal_row_to_the_scope_code() -> None:
    row = "2026-10-05T19:00:00Z | STATUS | lane=x | handoff-refused=r#1 | text\n"
    with pytest.raises(ValidationError, match="refusal_row"):
        ModelPrHandoffDecision(
            verdict=EnumPrHandoffVerdict.REFUSE,
            error_code=E.REPO_OUT_OF_LANDING_SCOPE,
        )
    with pytest.raises(ValidationError, match="refusal_row"):
        ModelPrHandoffDecision(
            verdict=EnumPrHandoffVerdict.REFUSE,
            error_code=E.HELD,
            refusal_row=row,
        )


def test_contract_declares_no_protocol_dependency() -> None:
    contract = (
        Path(__file__).parents[4]
        / "src/omnimarket/nodes/node_pr_handoff_decision_compute/contract.yaml"
    ).read_text(encoding="utf-8")
    assert "protocol_dependencies" not in contract


def test_decisions_are_byte_identical_across_evaluations() -> None:
    scope = _scope(OVERLAY_NOT_LANDING_THE_REPO)
    assert _decide(scope).model_dump_json() == _decide(scope).model_dump_json()
    ready = _scope(OVERLAY_LANDING_THE_REPO)
    assert _decide(ready).model_dump_json() == _decide(ready).model_dump_json()
