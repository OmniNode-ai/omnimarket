# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dispatch plan sends no lane for a ticket whose work is already built; the closer takes it with its evidence (OMN-23376).

AC1: a ticket a merged change already names is not dispatched, whatever the watcher or a clone says about it, and it goes to the
closer. AC2: a ticket a lane reported already built with passing falsifiers goes to the closer with that evidence and is not
dispatched again, however often the fill runs. The failing controls are the first three tests: before this change the plan
had no merged or built facts and dispatched each of those tickets on every run.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.handlers.rules_lab_fill_closer import (
    CLOSER_NODE,
    built_reports_from_ledger,
    closer_route,
    merged_by_ticket,
    merges_from_clone_log,
)
from omnimarket.models.lab_fill.model_lab_fill_closer import (
    EnumLabFillCloserReason,
    ModelLabFillBuiltReport,
)
from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_dispatch_plan import (
    HandlerLabFillDispatchPlan,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillCapacityResult,
    ModelLabFillDispatchPlanRequest,
    ModelLabFillHostCapacity,
    ModelLabFillPlanConfig,
)

pytestmark = pytest.mark.unit

LEDGER_BUILT = (
    "2026-11-09T14:29:11Z | TERMINAL | lane=fill-ticket-OMN-22568-20261109T1419Z | ticket=OMN-22568 | outcome=skipped-already-built"
    " | falsifiers=pass | evidence=pytest tests/nodes/node_x -k ac1 exit 0 on dev a4798bc9d | already built, nothing changed"
)


def _ticket(ticket: str, **over: Any) -> dict[str, object]:
    return {
        "kind": "ticket",
        "ticket": ticket,
        "under_parent": True,
        "title": f"work {ticket}",
        **over,
    }


def _plan(candidates: list[dict[str, object]], lanes: int = 4, **request: Any) -> Any:
    host = ModelLabFillHostCapacity(
        name="h201",
        lanes=lanes,
        idle_lanes=lanes,
        runner_slots=lanes,
        cap_bound=False,
        codex=True,
        claude=True,
        running=0,
        load_per_core=0.1,
        reason="",
    )
    return HandlerLabFillDispatchPlan().handle(
        ModelLabFillDispatchPlanRequest(
            candidates=tuple(candidates),
            capacity=ModelLabFillCapacityResult(
                hosts=(host,), free=lanes, budget=lanes
            ),
            config=ModelLabFillPlanConfig(
                parent_ticket="OMN-23376", milestone="m", run_key="2026-11-09T1830Z"
            ),
            **request,
        )
    )


def test_a_ticket_a_merged_change_names_is_not_dispatched_and_goes_to_the_closer() -> (
    None
):
    plan = _plan(
        [_ticket("OMN-22623"), _ticket("OMN-5")],
        merged_prs={"OMN-22623": ("omnimarket#5177",)},
    )
    assert [i.ticket for i in plan.dispatch] == ["OMN-5"]
    assert [(r.ticket, r.reason, r.prs, r.closer) for r in plan.closer] == [
        (
            "OMN-22623",
            EnumLabFillCloserReason.IMPLEMENTATION_MERGED,
            ("omnimarket#5177",),
            CLOSER_NODE,
        )
    ]
    assert [(s.id, s.reason) for s in plan.skipped] == [
        ("OMN-22623", "implementation-merged")
    ]


def test_a_ticket_reported_already_built_with_passing_falsifiers_is_not_dispatched_again() -> (
    None
):
    reports = built_reports_from_ledger([LEDGER_BUILT])
    plan = _plan([_ticket("OMN-22568")], built_reports=reports)
    assert plan.dispatch == ()
    (route,) = plan.closer
    assert route.reason is EnumLabFillCloserReason.ALREADY_BUILT
    assert route.lane == "fill-ticket-OMN-22568-20261109T1419Z"
    assert "exit 0 on dev a4798bc9d" in route.evidence


def test_the_same_ticket_is_not_dispatched_on_any_of_a_days_runs() -> None:
    reports = built_reports_from_ledger([LEDGER_BUILT])
    runs = [_plan([_ticket("OMN-22568")], built_reports=reports) for _ in range(48)]
    assert sum(len(run.dispatch) for run in runs) == 0
    assert all(len(run.closer) == 1 for run in runs)


def test_with_no_merged_or_built_fact_the_ticket_is_dispatched() -> None:
    plan = _plan([_ticket("OMN-22568")])
    assert [i.ticket for i in plan.dispatch] == ["OMN-22568"]
    assert plan.closer == ()


def test_an_already_built_report_whose_falsifiers_did_not_pass_stops_nothing() -> None:
    line = LEDGER_BUILT.replace("falsifiers=pass", "falsifiers=fail")
    plan = _plan(
        [_ticket("OMN-22568")], built_reports=built_reports_from_ledger([line])
    )
    assert [i.ticket for i in plan.dispatch] == ["OMN-22568"]
    assert plan.closer == ()


def test_an_already_built_report_with_no_falsifiers_cell_stops_nothing() -> None:
    line = LEDGER_BUILT.replace(" | falsifiers=pass", "")
    assert built_reports_from_ledger([line])[0].falsifiers_passed is False


@pytest.mark.parametrize("kind", ["pr-red", "pr-stalled"])
def test_a_pr_lane_is_not_stopped_by_a_merge_naming_its_ticket(kind: str) -> None:
    candidate = {
        "kind": kind,
        "ticket": "OMN-9",
        "pr": "omnimarket#7",
        "repo": "omnimarket",
        "head_ref": "b7",
        "under_parent": True,
        "title": "red",
    }
    plan = _plan([candidate], merged_prs={"OMN-9": ("omnimarket#5",)})
    assert [i.kind for i in plan.dispatch] == [kind]
    assert plan.closer == ()


def test_a_dod_verify_item_is_the_closers_work_not_a_lane() -> None:
    item = {
        "kind": "dod-verify",
        "origin_kind": "ticket",
        "ticket": "OMN-22651",
        "under_parent": True,
        "title": "implementation merged in omnimarket#5187: piece 1",
    }
    plan = _plan([item], merged_prs={"OMN-22651": ("omnimarket#5187",)})
    assert plan.dispatch == ()
    assert [r.ticket for r in plan.closer] == ["OMN-22651"]


def test_a_merged_ticket_does_not_take_a_slot_another_ticket_could_use() -> None:
    plan = _plan(
        [_ticket("OMN-1"), _ticket("OMN-2"), _ticket("OMN-3")],
        lanes=1,
        merged_prs={"OMN-1": ("omnimarket#1",)},
    )
    assert [i.ticket for i in plan.dispatch] == ["OMN-2"]


def test_the_request_refuses_a_malformed_built_report() -> None:
    with pytest.raises(ValidationError):
        ModelLabFillBuiltReport(ticket="22568", lane="x", reported_at="t")


def test_clone_history_reads_a_numbered_squash_and_skips_an_evidence_commit() -> None:
    log = "\n".join(
        [
            "a1b2c3d4e5f60718\tfix(OMN-22623): the trigger accepts the key (#5177)",
            "0f0f0f0f0f0f0f0f\tevidence(OMN-22623): bind the receipt (#901)",
            "123456789abcdef0\tfeat(OMN-22651, OMN-22626): both",
            "ffffffffffffffff\tchore: nothing to see",
        ]
    )
    found = merges_from_clone_log(
        "omnimarket",
        log,
        ["OMN-22623", "OMN-22651", "OMN-1"],
        evidence_prefix="evidence(",
    )
    assert found == {
        "OMN-22623": ("omnimarket#5177",),
        "OMN-22651": ("omnimarket@123456789",),
    }
    assert merged_by_ticket([found, {"OMN-22623": ("omnibase_core#9",)}])[
        "OMN-22623"
    ] == ("omnibase_core#9", "omnimarket#5177")


def test_the_route_rule_alone_is_none_for_a_ticket_with_no_fact() -> None:
    assert closer_route("OMN-1", "ticket") is None


def test_golden_chain_the_contracts_plan_operation_routes_a_merged_ticket_to_the_closer() -> (
    None
):
    """The operation the contract binds, resolved the way the runtime resolves it, answers the closer route."""
    import importlib
    from importlib.resources import files

    import yaml

    contract = yaml.safe_load(
        files("omnimarket.nodes.node_lab_fill_plan_compute")
        .joinpath("contract.yaml")
        .read_text()
    )
    entry = next(
        e
        for e in contract["handler_routing"]["handlers"]
        if e["operation"] == "plan_lab_fill_dispatch"
    )
    handler = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    module, _, name = str(entry["input_model"]).rpartition(".")
    request_type = getattr(importlib.import_module(module), name)
    host = ModelLabFillHostCapacity(
        name="h201",
        lanes=1,
        idle_lanes=1,
        runner_slots=1,
        cap_bound=False,
        codex=True,
        claude=True,
        running=0,
        load_per_core=0.1,
        reason="",
    )
    request = request_type(
        candidates=(_ticket("OMN-22623"),),
        capacity=ModelLabFillCapacityResult(hosts=(host,), free=1, budget=1),
        config=ModelLabFillPlanConfig(
            parent_ticket="OMN-23376", milestone="m", run_key="2026-11-09T1830Z"
        ),
        merged_prs={"OMN-22623": ("omnimarket#5177",)},
    )
    result = handler().handle(request)
    dumped = result.model_dump(mode="json", by_alias=True, exclude_unset=True)
    assert dumped["dispatch"] == []
    assert dumped["closer"] == [
        {
            "ticket": "OMN-22623",
            "reason": "implementation-merged",
            "prs": ["omnimarket#5177"],
            "lane": "",
            "evidence": "",
            "closer": CLOSER_NODE,
        }
    ]


@pytest.mark.parametrize(
    "bad",
    [
        {"merged_prs": {"OMN-1": "omnimarket#1"}},
        {"built_reports": ({"ticket": "OMN-1", "lane": "x"},)},
        {"built_reports": ({"ticket": "bad", "lane": "x", "reported_at": "t"},)},
        {"closer_exempt_kinds": "pr-red"},
    ],
)
def test_error_chain_the_plan_request_refuses_a_malformed_closer_fact(
    bad: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        _plan([_ticket("OMN-1")], **bad)


def test_the_brief_tells_a_lane_the_shape_the_fill_reads_and_a_report_in_it_routes() -> (
    None
):
    """The cells the brief names are the cells the ledger reader takes, so a lane that follows the brief reaches the closer."""
    from omnimarket.models.lab_fill import ALREADY_BUILT_LINE

    assert "outcome=skipped-already-built" in ALREADY_BUILT_LINE
    assert "falsifiers=pass" in ALREADY_BUILT_LINE
    assert "evidence=" in ALREADY_BUILT_LINE
    row = (
        "2026-11-09T19:00:00Z | TERMINAL | lane=lab-fill-omn7-1900 | ticket=OMN-7 | outcome=skipped-already-built"
        " | falsifiers=pass | evidence=pytest -k ac1 exit 0"
    )
    plan = _plan([_ticket("OMN-7")], built_reports=built_reports_from_ledger([row]))
    assert plan.dispatch == ()
    assert [r.lane for r in plan.closer] == ["lab-fill-omn7-1900"]
