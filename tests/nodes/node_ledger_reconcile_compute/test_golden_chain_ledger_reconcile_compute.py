# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves and each operation runs through its binding (OMN-20677)."""

from __future__ import annotations

from omnimarket.models.ledger_reconcile import (
    ModelAppendOutcome,
    ModelPrFact,
    ModelPushFact,
    ModelReconcileDecision,
    ModelReconcileFacts,
    ModelReconcileParams,
    ModelReconcileRenderRequest,
    ModelReconcileResult,
)

from .support import ROWS, bindings, contract, decide_request

OPERATIONS = {"decide_ledger_reconcile", "render_ledger_reconcile_result"}
MERGED = ModelPrFact(
    repo="omnimarket",
    number=11,
    state="MERGED",
    merged_at="2026-09-22T11:00:00Z",
    merge_sha="a" * 40,
    title="fix(OMN-1): claimed work",
)
OPEN = ModelPrFact(
    repo="omnimarket", number=12, state="OPEN", title="fix(OMN-1): claimed work"
)


def test_contract_declares_the_bus_route_and_compute_shape() -> None:
    declared = contract()
    assert declared["name"] == "node_ledger_reconcile_compute"
    assert declared["node_type"] == "compute"
    assert declared["descriptor"]["purity"] == "pure"
    assert declared["descriptor"]["side_effects"] == []
    bus = declared["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.ledger-reconcile-decide-requested.v1"
    ]
    assert bus["publish_topics"] == ["onex.evt.omnimarket.ledger-reconcile-decided.v1"]
    assert declared["terminal_event"] in bus["publish_topics"]
    assert declared["runtime_dispatch"]["command_topic"] == bus["subscribe_topics"][0]
    routing = declared["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert {e["operation"] for e in routing["handlers"]} == OPERATIONS


def test_every_binding_resolves_to_a_definition_b_handler() -> None:
    for operation, (handler_type, request_type, result_type) in bindings().items():
        assert callable(handler_type.handle), operation
        assert request_type.model_config["frozen"], operation
        assert result_type.model_config["frozen"], operation


def test_a_decision_asks_for_what_it_lacks_then_decides_then_reports() -> None:
    """The three claims need PR facts; the open PR then needs a push time; then the rows are planned."""
    by_op = bindings()
    decide_type, decide_request_type, decision_type = by_op["decide_ledger_reconcile"]
    render_type, render_request_type, result_type = by_op[
        "render_ledger_reconcile_result"
    ]
    params = ModelReconcileParams(apply=True)

    first = decide_type().handle(decide_request(params=params))
    assert isinstance(first, decision_type)
    assert isinstance(first, ModelReconcileDecision)
    assert first.status == "needs-evidence"
    assert [(p.repo, p.number) for p in first.wanted.prs] == [
        ("omnimarket", 11),
        ("omnimarket", 12),
    ]
    assert first.planned == ()

    second = decide_type().handle(
        decide_request(params=params, facts=ModelReconcileFacts(prs=(MERGED, OPEN)))
    )
    assert second.status == "needs-evidence"
    assert [(p.repo, p.number) for p in second.wanted.pushes] == [("omnimarket", 12)]

    facts = ModelReconcileFacts(
        prs=(MERGED, OPEN),
        pushes=(ModelPushFact(repo="omnimarket", number=12),),
    )
    request = decide_request(params=params, facts=facts)
    assert isinstance(request, decide_request_type)
    decided = decide_type().handle(request)
    assert decided.status == "decided"
    assert decided.wanted.is_empty()
    assert [(p.index, p.kind) for p in decided.planned] == [
        (0, "terminal"),
        (1, "attention"),
    ]
    assert "RECONCILER-AUTO-CLOSE" in decided.planned[0].row
    assert "RECONCILER-NEEDS-ATTENTION" in decided.planned[1].row

    rendered = render_type().handle(
        ModelReconcileRenderRequest(
            decide=request,
            outcomes=tuple(ModelAppendOutcome(index=p.index) for p in decided.planned),
        )
    )
    assert isinstance(rendered, result_type)
    assert isinstance(rendered, ModelReconcileResult)
    assert render_request_type is ModelReconcileRenderRequest
    assert (rendered.exit_code, rendered.status) == (1, "reconciled")
    assert (rendered.dangling, rendered.auto_closed, rendered.needs_attention) == (
        3,
        1,
        1,
    )
    assert rendered.unknown == 1
    assert "[terminal-appended]" in rendered.stdout
    assert "[attention-appended]" in rendered.stdout


def test_a_clean_ledger_decides_nothing_and_reports_clean() -> None:
    by_op = bindings()
    decision = by_op["decide_ledger_reconcile"][0]().handle(decide_request(rows=[]))
    assert decision.status == "decided"
    assert decision.planned == ()
    result = by_op["render_ledger_reconcile_result"][0]().handle(
        ModelReconcileRenderRequest(decide=decide_request(rows=[]))
    )
    assert (result.exit_code, result.status, result.dangling) == (0, "clean", 0)
    assert len(ROWS) == 3
