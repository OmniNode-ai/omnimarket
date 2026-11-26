# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A live claim owns a plan by what it names, never by what its lane is called (OMN-26204).

A rule lists tickets, plan paths and pull requests of the plan; an open claim owns the plan
when its ``ticket=``, ``plan=`` or ``pr=`` cell is listed and its newest row is inside the
rule's window. The lane-name prefix is gone from the rule. Every value is the overlay's: the
handler holds no ticket, path or repository literal.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.plan_owner_check import (
    EnumPlanOwnerVerdict,
    ModelPlanOwnerOverlay,
    ModelPlanOwnerRequest,
    ModelPlanOwnerRule,
)
from omnimarket.nodes.node_plan_owner_check_effect.handlers import (
    handler_plan_owner_check,
)
from omnimarket.nodes.node_plan_owner_check_effect.handlers.handler_plan_owner_check import (
    HandlerPlanOwnerCheck,
)
from omnimarket.nodes.node_plan_owner_check_effect.overlay import load_overlay
from omnimarket.nodes.node_work_ledger_query_effect.handlers import (
    work_ledger_query_replay as replay,
)
from tests.unit.nodes.node_plan_owner_check_effect.test_plan_owner_chain_events import (
    HEADING,
    NOW,
    UNKNOWN_FIX,
    ListSink,
    claim_line,
    node_answer,
    request,
)
from tests.unit.nodes.node_plan_owner_check_effect.test_plan_owner_reader_seam import (
    verdict_over,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    FakeStateReader,
    claim_entity,
    record,
)

pytestmark = pytest.mark.unit

FINDING = "cutover-plan-owner"
LIVE = "2026-11-14T14:30:00Z"
PLAN_TICKET = "OMN-70001"
PLAN_PATH = "2026-10-29-example-plan"
PLAN_PR = "example-repo#901"


def plan_rule(**changes: object) -> ModelPlanOwnerRule:
    base = ModelPlanOwnerRule(
        finding=FINDING,
        tickets=(PLAN_TICKET,),
        plan_paths=(PLAN_PATH,),
        prs=(f"ExampleOrg/{PLAN_PR}",),
        live_window_minutes=180,
        why="no live CLAIM names the plan's ticket, plan path or pull request",
        fix="open a lane whose CLAIM carries the plan's ticket",
        unknown_fix=UNKNOWN_FIX,
    )
    return base.model_copy(update=changes)


def check(rule: ModelPlanOwnerRule, *claims: str) -> EnumPlanOwnerVerdict:
    overlay = ModelPlanOwnerOverlay(
        reader="cutover-owner", claims_window_hours=3, rules=(rule,), journal_env="J"
    )
    handler = HandlerPlanOwnerCheck(overlay, ListSink(), clock=lambda: NOW)
    return handler.handle(request(node_answer((HEADING, *claims)))).verdict


@pytest.mark.parametrize(
    ("cell", "value"),
    [
        ("ticket", PLAN_TICKET),
        ("pr", PLAN_PR),
        ("pr", f"ExampleOrg/{PLAN_PR}"),
        ("plan", PLAN_PATH),
        ("plan", f"beta/plans/{PLAN_PATH}.md"),
        ("plan", f"beta/other-plan.md,{PLAN_PATH}"),
    ],
)
def test_claim_names_plan_owned_for_a_lane_no_prefix_ever_matched(
    cell: str, value: str
) -> None:
    claim = claim_line("ledgercut-2140", LIVE, **{cell: value})
    assert check(plan_rule(), claim) is EnumPlanOwnerVerdict.OWNED


def test_claim_names_plan_none_of_the_three_reads_missing() -> None:
    claim = claim_line("ledgercut-2140", LIVE)
    assert check(plan_rule(), claim) is EnumPlanOwnerVerdict.MISSING


def test_claim_names_plan_another_plans_ticket_path_and_pr_read_missing() -> None:
    claim = claim_line(
        "ledgercut-2140",
        LIVE,
        ticket="OMN-70002",
        pr="example-repo#902",
        plan="beta/plans/some-other-plan.md",
    )
    assert check(plan_rule(), claim) is EnumPlanOwnerVerdict.MISSING


def test_prefix_does_not_own_a_ledger_cut_lane_that_names_nothing() -> None:
    claim = claim_line("ledger-cut-x", LIVE)
    handler = HandlerPlanOwnerCheck(
        ModelPlanOwnerOverlay(
            reader="cutover-owner",
            claims_window_hours=3,
            rules=(plan_rule(),),
            journal_env="J",
        ),
        ListSink(),
        clock=lambda: NOW,
    )
    verdict = handler.handle(request(node_answer((HEADING, claim))))
    assert verdict.verdict is EnumPlanOwnerVerdict.MISSING
    assert verdict.owners == ()


def test_prefix_does_not_own_a_rule_cannot_carry_lane_prefixes() -> None:
    with pytest.raises(ValidationError, match="lane_prefixes"):
        ModelPlanOwnerRule.model_validate(
            {
                "finding": FINDING,
                "lane_prefixes": ("ledger-cut",),
                "tickets": (PLAN_TICKET,),
                "live_window_minutes": 60,
                "why": "w",
                "fix": "f",
                "unknown_fix": "u",
            }
        )


def test_overlay_values_only_the_rule_decides_ticket_path_pr_and_window() -> None:
    owned_by_ticket = claim_line("any-lane", LIVE, ticket="OMN-70099")
    owned_by_plan = claim_line("any-lane", LIVE, plan="plans/another-plan.md")
    owned_by_pr = claim_line("any-lane", LIVE, pr="other-repo#5")
    other = plan_rule(
        tickets=("OMN-70099",), plan_paths=("another-plan",), prs=("other-repo#5",)
    )
    for claim in (owned_by_ticket, owned_by_plan, owned_by_pr):
        assert check(plan_rule(), claim) is EnumPlanOwnerVerdict.MISSING
        assert check(other, claim) is EnumPlanOwnerVerdict.OWNED
    live_claim = claim_line("any-lane", LIVE, ticket=PLAN_TICKET)
    assert check(plan_rule(), live_claim) is EnumPlanOwnerVerdict.OWNED
    assert (
        check(plan_rule(live_window_minutes=20), live_claim)
        is EnumPlanOwnerVerdict.MISSING
    )


def test_overlay_values_only_the_handler_source_holds_no_ticket_path_or_repo() -> None:
    tree = ast.parse(Path(handler_plan_owner_check.__file__).read_text())
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    assert literals, "positive control: the handler holds string literals"
    for text in literals:
        assert "OMN-" not in text, text
        assert "omni" not in text.lower(), text
        assert "ledger-cut" not in text, text
        assert "plans/" not in text, text


def test_a_stale_claim_that_names_the_plan_owns_nothing() -> None:
    stale = claim_line("ledgercut-2140", "2026-11-14T09:00:00Z", ticket=PLAN_TICKET)
    assert check(plan_rule(), stale) is EnumPlanOwnerVerdict.MISSING
    fresh = claim_line("ledgercut-2140", LIVE, ticket=PLAN_TICKET)
    assert check(plan_rule(), fresh) is EnumPlanOwnerVerdict.OWNED


def test_a_terminaled_claim_is_not_open_and_owns_nothing() -> None:
    claim = (
        "2026-11-14T10:00:00Z | CLAIM | lane=ledgercut-2140 | ticket=OMN-70001 "
        "| pr=example-repo#901 | plan=plans/2026-10-29-example-plan.md | host=h1"
    )
    terminal = (
        "2026-11-14T11:00:00Z | TERMINAL | lane=ledgercut-2140 | ticket=OMN-70001 "
        "| outcome=handed-off"
    )
    open_rows = replay.parse_rows([record(claim, "r1")])
    [open_claim] = replay.open_claims(open_rows)
    assert (open_claim.ticket, open_claim.pr, open_claim.plan) == (
        "OMN-70001",
        "example-repo#901",
        "plans/2026-10-29-example-plan.md",
    )
    closed_rows = replay.parse_rows([record(claim, "r1"), record(terminal, "r2")])
    assert replay.open_claims(closed_rows) == []
    # The open-claims answer of the closed run lists no claim, so the rule sees none.
    assert check(plan_rule()) is EnumPlanOwnerVerdict.MISSING


def test_a_rule_with_no_matcher_is_refused_typed() -> None:
    with pytest.raises(
        ValidationError, match="names no ticket, plan path or pull request"
    ):
        ModelPlanOwnerRule(
            finding=FINDING,
            live_window_minutes=60,
            why="w",
            fix="f",
            unknown_fix="u",
        )


def test_a_rule_with_no_matcher_is_refused_at_load(tmp_path: Path) -> None:
    path = tmp_path / "owner.yaml"
    path.write_text(
        "reader: cutover-owner\nclaims_window_hours: 6\njournal_env: J\nrules:\n"
        "  - finding: cutover-plan-owner\n    live_window_minutes: 60\n"
        "    why: w\n    fix: f\n    unknown_fix: u\n"
    )
    with pytest.raises(
        NodeOverlayError,
        match=r"is invalid for .*\(ValidationError\): fields \['rules'",
    ):
        load_overlay({"ONEX_PLAN_OWNER_CHECK_OVERLAY": str(path)})


def test_a_lane_prefix_in_an_overlay_is_refused_at_load(tmp_path: Path) -> None:
    path = tmp_path / "owner.yaml"
    path.write_text(
        "reader: cutover-owner\nclaims_window_hours: 6\njournal_env: J\nrules:\n"
        "  - finding: cutover-plan-owner\n    lane_prefixes: [ledger-cut]\n"
        "    tickets: [OMN-70001]\n    live_window_minutes: 60\n"
        "    why: w\n    fix: f\n    unknown_fix: u\n"
    )
    with pytest.raises(NodeOverlayError, match="lane_prefixes"):
        load_overlay({"ONEX_PLAN_OWNER_CHECK_OVERLAY": str(path)})


@pytest.mark.parametrize("pr", ["no-hash", "#12", "repo#", "a b#1"])
def test_a_malformed_pull_request_in_a_rule_is_refused(pr: str) -> None:
    with pytest.raises(ValidationError):
        ModelPlanOwnerRule(
            finding=FINDING,
            prs=(pr,),
            live_window_minutes=60,
            why="w",
            fix="f",
            unknown_fix="u",
        )


def test_the_old_line_shape_without_pr_and_plan_cells_is_unknown_never_missing() -> (
    None
):
    old = (
        "OPEN-CLAIM lane=ledgercut-2140 claim=2026-11-14T14:00:00Z db:row-1 "
        f"ticket={PLAN_TICKET} newest-row={LIVE} kind=STATUS age=0h30m rows=4 "
        "host=h051 run=r1 leases=none liveness=unknown"
    )
    handler = HandlerPlanOwnerCheck(
        ModelPlanOwnerOverlay(
            reader="cutover-owner",
            claims_window_hours=3,
            rules=(plan_rule(),),
            journal_env="J",
        ),
        ListSink(),
        clock=lambda: NOW,
    )
    verdict = handler.handle(
        ModelPlanOwnerRequest(finding=FINDING, node_answer=node_answer((HEADING, old)))
    )
    assert verdict.verdict is EnumPlanOwnerVerdict.UNKNOWN
    assert verdict.reason == "unparseable-claim-line"


def test_claim_names_plan_through_the_real_reader_from_the_claim_row() -> None:
    """The CLAIM row's ``pr=`` and ``plan=`` cells ride the rendered line to the verdict."""
    raw = (
        "2026-11-01T09:00:00Z | CLAIM | lane=ledgercut-2140 | ticket=OMN-70002 "
        "| pr=example-repo#901 | plan=beta/plans/2026-10-29-example-plan.md "
        "| host=h051 | run=r1 | scope=anything"
    )
    entity = claim_entity().model_copy(
        update={"lane": "ledgercut-2140", "opened_row": record(raw, "row-claim-pr")}
    )
    answer, verdict = verdict_over(
        FakeStateReader((entity,)),
        tickets=("OMN-1",),
        plan_paths=(),
        prs=("ExampleOrg/example-repo#901",),
    )
    assert (
        "ticket=OMN-70002 pr=example-repo#901 "
        "plan=beta/plans/2026-10-29-example-plan.md newest-row="
    ) in answer.lines[1]
    assert verdict.verdict is EnumPlanOwnerVerdict.OWNED
    assert verdict.owners == ("ledgercut-2140",)
    _, by_plan = verdict_over(
        FakeStateReader((entity,)),
        tickets=("OMN-1",),
        plan_paths=("2026-10-29-example-plan",),
        prs=(),
    )
    assert by_plan.verdict is EnumPlanOwnerVerdict.OWNED
    _, none = verdict_over(
        FakeStateReader((entity,)), tickets=("OMN-1",), plan_paths=("x",), prs=()
    )
    assert none.verdict is EnumPlanOwnerVerdict.MISSING
