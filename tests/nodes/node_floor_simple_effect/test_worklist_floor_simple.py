# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The board lists every in-scope item that needs help, each with its owner state (OMN-23184).

The operator, 2026-11-08 about 23:00Z: the board is meant to be the complete work list, but its red pull request row read
"Holding 1 (or 2) pull requests" all evening while the throughput tick counted 60 to 70. The cause is the reader's filter:
``ModelGoalScope.classify`` knows the goal's lists, and the landing controller also admits a pull request by its ticket's chain
to a root, so the floor called every ticket-rule pull request unlisted and left it out. The fixtures are tonight's watcher
state (212 open non-draft pull requests, held-excluded ones left out; ``pr_watcher_open_2026-11-08T2305Z.json``) and the
goal_scope lists cut to the pull requests that state holds. The Linear chain of a ticket is not on the lab host, so the
tests declare which tickets descend from a root; that declaration is the injected scope fact, not a Linear read.
The failing control is the same state read without the projection, which draws the red row from 2 pull requests.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.floor_17a import (
    ModelFloorLane,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelFloorSimpleOverlay,
    ModelGoalScope,
)
from omnimarket.models.floor_17a.model_worklist import (
    ModelWorklistItemIn,
    ModelWorklistOverlay,
)
from omnimarket.models.work_state_projection import (
    ModelPullRequestSource,
    ModelTicketSource,
    ModelWorkScope,
    ModelWorkStateRequest,
    ModelWorkStateResult,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
    build_worklist,
    check_red_rule,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import read_watcher
from omnimarket.nodes.node_floor_simple_effect.handlers.worklist_sources import (
    projected_in_scope_prs,
    read_controller_owners,
    read_worklist,
)
from omnimarket.nodes.node_work_state_projection_compute.handlers.handler_work_state_projection import (
    HandlerWorkStateProjection,
)
from tests.nodes.node_floor_simple_effect.support import (
    NOW,
    OVERLAYS,
    build_home,
    environ,
    overlay_text,
)
from tests.nodes.node_floor_simple_effect.test_golden_chain_floor_simple import (
    run,
    written,
)
from tests.nodes.node_floor_simple_effect.test_work_state_lanes_floor_simple import (
    RULES,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
WATCHER = FIXTURES / "pr_watcher_open_2026-11-08T2305Z.json"
SCOPE = ModelGoalScope.model_validate(
    json.loads((FIXTURES / "goal_scope_2026-11-08T2305Z.json").read_text("utf-8"))
)
WATCHED = json.loads(WATCHER.read_text("utf-8"))
TICK = datetime(2026, 11, 8, 23, 5, 52, tzinfo=UTC)
# The tickets named by these repositories' pull requests are declared to descend from a goal root (the injected fact); any
# pull request naming one of those tickets is then in scope, in whatever repository it sits.
RULE_REPOS = ("omnimarket", "omnibase_infra", "omniclaude")
OVERLAY = ModelWorklistOverlay(
    titles={
        "pr": "Pull requests that are red or conflicting",
        "stopped": "Tickets a lane left stopped",
        "shared_red": "Required checks red from a shared cause nobody owns",
    },
    page_size=25,
    shared_red_after_s=3600,
    stopped_follow_up_cells=("follow_up",),
)
CONFLICT = ("DIRTY",)


def ticket_of(title: str) -> str:
    found = re.search(r"OMN-\d+", title)
    return found.group(0) if found else ""


def projection(
    tickets_under_root: set[str], extra: tuple[ModelTicketSource, ...] = ()
) -> ModelWorkStateResult:
    """The real projection node over tonight's pull requests, with the declared tickets one step under the goal's root."""
    prs: list[ModelPullRequestSource] = []
    for value in WATCHED["prs"].values():
        facts = value["facts"]
        prs.append(
            ModelPullRequestSource(
                repo=facts["repo"],
                number=facts["number"],
                title=facts["title"],
                ticket=ticket_of(facts["title"]) or None,
                state="OPEN",
                red_required_checks=tuple(value["ci"]["red"]),
                controller_decision=value["cls"],
            )
        )
    tickets = tuple(
        ModelTicketSource(ticket_id=t, state_type="started", parent="OMN-22478")
        for t in sorted(tickets_under_root)
    )
    return HandlerWorkStateProjection().handle(
        ModelWorkStateRequest(
            now=NOW,
            rules=RULES,
            scope=ModelWorkScope(in_scope_tickets=("OMN-22478",)),
            tickets=(*tickets, *extra),
            pull_requests=tuple(prs),
        )
    )


def declared_tickets() -> set[str]:
    return {
        ticket_of(v["facts"]["title"])
        for k, v in WATCHED["prs"].items()
        if k.partition("#")[0] in RULE_REPOS and ticket_of(v["facts"]["title"])
    }


def hand_count_red() -> int:
    """The red open non-draft pull requests in scope, counted by hand from the state: the listed ones plus the declared repos."""
    n = 0
    declared = declared_tickets()
    for key, value in WATCHED["prs"].items():
        listed = SCOPE.classify(key) == "in_scope"
        ruled = (
            ticket_of(value["facts"]["title"]) in declared
            and SCOPE.classify(key) != "paused"
        )
        if value["cls"] == "red" and (listed or ruled):
            n += 1
    return n


def read(projected: frozenset[str]) -> Any:
    return read_watcher(
        WATCHER,
        TICK,
        excluded_classes=("held-excluded",),
        queue_classes=("green-unarmed",),
        merge_window_minutes=60,
        scope=SCOPE,
        projected=projected,
    )


def write_state(
    path: Path, leases: list[dict[str, Any]], causes: list[dict[str, Any]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"leases": leases, "causes": causes}), encoding="utf-8")


# --- why prs:red undercounted ---------------------------------------------------------------------------------------------


def test_error_control_the_lists_alone_hold_two_red_pull_requests_the_row_tonight() -> (
    None
):
    """The failing control: tonight's state through the reader with no projection draws 'Holding 2 pull requests'."""
    reading = read(frozenset())
    assert reading.class_counts["red"] == 2
    assert hand_count_red() > 60


def test_golden_the_ticket_rule_admits_the_red_pull_requests_the_lists_leave_out() -> (
    None
):
    projected = projected_in_scope_prs(projection(declared_tickets()))
    reading = read(projected)
    assert reading.class_counts["red"] == hand_count_red()
    assert len(reading.class_prs["red"]) == reading.class_counts["red"]


def test_error_a_paused_pull_request_stays_paused_whatever_the_projection_says() -> (
    None
):
    paused = next(k for k in SCOPE.paused_prs if k in WATCHED["prs"])
    reading = read(frozenset({paused}))
    assert paused not in reading.class_prs.get(WATCHED["prs"][paused]["cls"], ())


# --- the three classes -----------------------------------------------------------------------------------------------------


def test_golden_every_red_or_conflicting_in_scope_pull_request_is_listed_with_its_owner(
    tmp_path: Path,
) -> None:
    proj = projection(declared_tickets())
    red = sorted(
        k
        for k, v in WATCHED["prs"].items()
        if v["cls"] == "red" and k in projected_in_scope_prs(proj)
    )
    landing_pr, cause_pr = red[0], red[1]
    state = tmp_path / "state.json"
    write_state(
        state,
        [
            {"pr": f"OmniNode-ai/{landing_pr}", "lease_id": 7},
            {"pr": "cause-key-1", "lease_id": 8},
        ],
        [{"key": "cause-key-1", "members": [{"pr": cause_pr}]}],
    )
    items = read_worklist(
        watcher_path=WATCHER,
        controller_state_path=state,
        projection=proj,
        scope=SCOPE,
        overlay=OVERLAY,
        excluded_classes=("held-excluded",),
        conflict_states=CONFLICT,
        now=TICK,
    )
    assert items is not None
    prs = {i.key: i for i in items if i.kind == "pr"}
    expected = {
        k
        for k, v in WATCHED["prs"].items()
        if k in projected_in_scope_prs(proj)
        and SCOPE.classify(k) != "paused"
        and (v["cls"] == "red" or v["review"]["merge_state"] == "DIRTY")
    }
    assert set(prs) == expected
    assert prs[landing_pr].landing_lease == "landing-L7"
    assert prs[cause_pr].cause_lease.startswith("cause-L8")
    assert prs[landing_pr].red_checks == tuple(WATCHED["prs"][landing_pr]["ci"]["red"])
    assert (prs[landing_pr].age_s or 0) > 0
    conflicting = [i for i in prs.values() if i.conflicting]
    assert conflicting
    assert all(
        WATCHED["prs"][i.key]["review"]["merge_state"] == "DIRTY" for i in conflicting
    )


def test_golden_a_green_pull_request_and_a_draft_are_not_listed(tmp_path: Path) -> None:
    proj = projection(declared_tickets())
    state = tmp_path / "state.json"
    write_state(state, [], [])
    items = read_worklist(
        watcher_path=WATCHER,
        controller_state_path=state,
        projection=proj,
        scope=SCOPE,
        overlay=OVERLAY,
        excluded_classes=("held-excluded",),
        conflict_states=CONFLICT,
        now=TICK,
    )
    assert items is not None
    green_clean = [
        k
        for k, v in WATCHED["prs"].items()
        if v["cls"] != "red" and v["review"]["merge_state"] != "DIRTY"
    ]
    assert green_clean
    listed = {i.key for i in items if i.kind == "pr"}
    assert not listed & set(green_clean)


def test_golden_a_stopped_ticket_with_a_named_follow_up_and_no_successor_is_listed() -> (
    None
):
    proj = projection(declared_tickets(), extra=())
    from omnimarket.models.work_state_projection import (
        EnumCriterionReading,
        EnumScopeStatus,
        EnumWorkItemKind,
        EnumWorkItemState,
        ModelWorkItem,
    )

    def stopped(ticket: str, left: str) -> ModelWorkItem:
        return ModelWorkItem(
            key=f"ticket:{ticket}",
            kind=EnumWorkItemKind.TICKET,
            title="a ticket",
            ticket=ticket,
            state=EnumWorkItemState.STOPPED,
            reason="lane l1 closed outcome=blocked",
            scope=EnumScopeStatus.IN_SCOPE,
            criterion=EnumCriterionReading.TRUE,
            left=left,
        )

    with_pr = next(iter(declared_tickets()))
    added = ModelWorkStateResult(
        as_of=proj.as_of,
        counts=proj.counts,
        items=(
            *proj.items,
            stopped("OMN-90001", "outcome=blocked; follow_up=OMN-90002"),
            stopped("OMN-90003", "outcome=blocked"),
            stopped(with_pr, "outcome=partial; follow_up=OMN-90004"),
        ),
    )
    state = Path(__file__).parent / "fixtures" / "unused"
    del state
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        write_state(path, [], [])
        items = read_worklist(
            watcher_path=WATCHER,
            controller_state_path=path,
            projection=added,
            scope=SCOPE,
            overlay=OVERLAY,
            excluded_classes=("held-excluded",),
            conflict_states=CONFLICT,
            now=TICK,
        )
    assert items is not None
    stopped_keys = {i.key for i in items if i.kind == "stopped"}
    # OMN-90003 names no follow-up; the ticket with an open pull request has a successor already.
    assert stopped_keys == {"OMN-90001"}


def test_golden_a_required_check_red_over_an_hour_on_several_pull_requests_is_a_shared_red(
    tmp_path: Path,
) -> None:
    proj = projection(declared_tickets())
    state = tmp_path / "state.json"
    write_state(state, [], [])
    items = read_worklist(
        watcher_path=WATCHER,
        controller_state_path=state,
        projection=proj,
        scope=SCOPE,
        overlay=OVERLAY,
        excluded_classes=("held-excluded",),
        conflict_states=CONFLICT,
        now=TICK,
    )
    assert items is not None
    shared = [i for i in items if i.kind == "shared_red"]
    assert shared
    for item in shared:
        assert len(item.prs) >= 2
        assert (item.age_s or 0) >= OVERLAY.shared_red_after_s
        assert item.cause_lease == ""
    # a younger window drops every one that is not yet an hour old
    short = OVERLAY.model_copy(update={"shared_red_after_s": 10**9})
    items_none = read_worklist(
        watcher_path=WATCHER,
        controller_state_path=state,
        projection=proj,
        scope=SCOPE,
        overlay=short,
        excluded_classes=("held-excluded",),
        conflict_states=CONFLICT,
        now=TICK,
    )
    assert items_none is not None
    assert not [i for i in items_none if i.kind == "shared_red"]


def test_golden_a_cause_lease_over_a_member_owns_the_shared_red(tmp_path: Path) -> None:
    proj = projection(declared_tickets())
    state = tmp_path / "state.json"
    write_state(state, [], [])
    base = read_worklist(
        watcher_path=WATCHER,
        controller_state_path=state,
        projection=proj,
        scope=SCOPE,
        overlay=OVERLAY,
        excluded_classes=("held-excluded",),
        conflict_states=CONFLICT,
        now=TICK,
    )
    assert base is not None
    target = next(i for i in base if i.kind == "shared_red")
    write_state(
        state,
        [{"pr": "shared-cause", "lease_id": 3}],
        [{"key": "shared-cause", "members": [{"pr": target.prs[0]}]}],
    )
    items = read_worklist(
        watcher_path=WATCHER,
        controller_state_path=state,
        projection=proj,
        scope=SCOPE,
        overlay=OVERLAY,
        excluded_classes=("held-excluded",),
        conflict_states=CONFLICT,
        now=TICK,
    )
    assert items is not None
    owned = next(i for i in items if i.key == target.key)
    assert owned.cause_lease.startswith("cause-L3")


# --- the owner state and the row ------------------------------------------------------------------------------------------


def item(key: str, **over: Any) -> ModelWorklistItemIn:
    base: dict[str, Any] = {
        "kind": "pr",
        "key": key,
        "title": key,
        "prs": (key,),
        "age_s": 100,
    }
    return ModelWorklistItemIn.model_validate(base | over)


def lane(
    name: str, pr: str = "", ticket: str = "", alive: str = "alive"
) -> ModelFloorLane:
    return ModelFloorLane(lane=name, pr=pr, ticket=ticket, liveness=alive)


def test_golden_owner_state_is_a_live_lane_then_a_landing_lease_then_a_cause_lease_else_nobody() -> (
    None
):
    classes = build_worklist(
        (
            item("r#1"),
            item("r#2", landing_lease="landing-L1"),
            item("r#3", cause_lease="cause-L2 k"),
            item("r#4", landing_lease="landing-L1"),
            item("r#5", ticket="OMN-7"),
        ),
        (lane("l1", pr="r#4"), lane("l2", ticket="OMN-7")),
        dict(OVERLAY.titles),
        25,
    )
    by = {i.key: i for c in classes for p in c.pages for i in p}
    assert by["r#1"].owner_state == "unowned"
    assert by["r#2"].owner_state == "landing_lease"
    assert by["r#3"].owner_state == "cause_lease"
    assert (by["r#4"].owner_state, by["r#4"].owner) == ("live_lane", "l1")
    assert (by["r#5"].owner_state, by["r#5"].owner) == ("live_lane", "l2")


def test_error_a_dead_or_finished_lane_owns_nothing() -> None:
    classes = build_worklist(
        (item("r#1"), item("r#2")),
        (lane("d", pr="r#1", alive="dead"), lane("f", pr="r#2", alive="finished")),
        dict(OVERLAY.titles),
        25,
    )
    assert classes[0].unowned == 2


def test_golden_members_are_paged_unowned_first_then_oldest() -> None:
    items = tuple(item(f"r#{n}", age_s=n) for n in range(1, 8))
    classes = build_worklist(items, (lane("l", pr="r#7"),), dict(OVERLAY.titles), 3)
    pr = classes[0]
    assert (pr.total, pr.unowned, pr.owned, len(pr.pages)) == (7, 6, 1, 3)
    assert [i.key for i in pr.pages[0]] == ["r#6", "r#5", "r#4"]
    assert pr.pages[-1][-1].key == "r#7"


def doc_for(
    items: tuple[ModelWorklistItemIn, ...] | None,
    lanes: tuple[ModelFloorLane, ...] = (),
) -> ModelFloorSimple:
    base = overlay_text("node_floor_simple_compute")
    base["worklist"] = OVERLAY.model_dump(mode="json")
    return HandlerFloorSimple().handle(
        ModelFloorSimpleInput(
            now=NOW,
            overlay=ModelFloorSimpleOverlay.model_validate(base),
            open_prs=10,
            open_prs_before=12,
            merges_in_window=4,
            watcher_read_at=NOW - timedelta(minutes=1),
            slots_working=5,
            slots_total=20,
            record_read_at=NOW - timedelta(minutes=1),
            lanes=lanes,
            lab=None,
            receipts=None,
            go_given_through=0,
            worklist=items,
        )
    )


def row(doc: ModelFloorSimple, kind: str) -> Any:
    return next(c for c in doc.causes if c.id == f"worklist:{kind}")


def test_golden_a_class_row_is_red_while_any_member_has_nobody_and_amber_when_all_are_owned() -> (
    None
):
    items = (item("r#1"), item("r#2"))
    red = doc_for(items, (lane("l1", pr="r#1"),))
    assert row(red, "pr").state == "red"
    assert row(red, "pr").work_under_way is False
    assert "1 of 2 have nobody on them: r#2" in row(red, "pr").detail
    assert check_red_rule(red, (lane("l1", pr="r#1"),)).violations == ()
    owned = doc_for(items, (lane("l1", pr="r#1"), lane("l2", pr="r#2")))
    assert row(owned, "pr").state == "amber"
    assert row(owned, "pr").lanes == ("l1", "l2")
    assert (
        check_red_rule(owned, (lane("l1", pr="r#1"), lane("l2", pr="r#2"))).violations
        == ()
    )
    assert owned.worklist is not None
    assert owned.worklist[0].owned == 2


def test_golden_one_unowned_item_among_sixty_owned_ones_keeps_the_row_red() -> None:
    """A class row that went amber when any one lane was on any member is how 'Holding 1' hid sixty."""
    items = tuple(item(f"r#{n}") for n in range(1, 62))
    lanes = (lane("l1", pr="r#1"),)
    doc = doc_for(items, lanes)
    assert row(doc, "pr").state == "red"
    assert doc.worklist is not None
    assert doc.worklist[0].unowned == 60
    assert doc.level == "red"


def test_error_no_worklist_in_the_input_draws_no_class_row() -> None:
    doc = doc_for(None)
    assert doc.worklist is None
    assert not [c for c in doc.causes if c.id.startswith("worklist:")]


def test_error_the_reading_fails_on_a_class_row_planted_amber_with_an_unowned_member() -> (
    None
):
    doc = doc_for((item("r#1"), item("r#2")), (lane("l1", pr="r#1"),))
    planted = doc.model_copy(
        update={
            "causes": tuple(
                c.model_copy(update={"state": "amber"}) if c.id == "worklist:pr" else c
                for c in doc.causes
            )
        }
    )
    verdict = check_red_rule(planted, (lane("l1", pr="r#1"),))
    assert any("worklist:pr draws amber" in v for v in verdict.violations)


def test_error_the_reading_fails_on_an_item_read_unowned_while_a_lane_is_on_it() -> (
    None
):
    doc = doc_for((item("r#1"),), ())
    verdict = check_red_rule(doc, (lane("l1", pr="r#1"),))
    assert any("r#1 reads unowned while l1 is on it" in v for v in verdict.violations)


# --- the board cycle -------------------------------------------------------------------------------------------------------


def cycle_home(
    tmp_path: Path, proj: ModelWorkStateResult | None, state: bool
) -> tuple[Path, dict[str, str]]:
    roots = tmp_path / "overlays"
    shutil.copytree(OVERLAYS, roots)
    overlay_path = roots / "node_floor_simple_compute" / "overlay.yaml"
    loaded = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    loaded["worklist"] = OVERLAY.model_dump(mode="json")
    overlay_path.write_text(yaml.safe_dump(loaded), encoding="utf-8")
    scope_path = roots / "goal_scope" / "overlay.yaml"
    scope_path.write_text(
        yaml.safe_dump(
            {
                "ruling": "a test goal",
                **{
                    k: list(v) if isinstance(v, tuple) else v
                    for k, v in SCOPE.model_dump().items()
                    if k != "ruling"
                },
            }
        ),
        encoding="utf-8",
    )
    home = build_home(tmp_path, watch=WATCHED)
    if proj is not None:
        path = home / ".onex_state/work-state/work-state.json"
        path.parent.mkdir(parents=True)
        path.write_text(proj.model_dump_json(), encoding="utf-8")
    if state:
        write_state(home / "landing/state.json", [], [])
    return home, environ(home) | {"ONEX_SKILL_OVERLAY_ROOTS": str(roots)}


def test_board_read_the_published_floor_lists_the_work_and_the_red_row_counts_it(
    tmp_path: Path,
) -> None:
    proj = projection(declared_tickets())
    home, env = cycle_home(tmp_path, proj, state=True)
    run(home, env=env)
    doc = written(home)
    assert doc.sources_read["worklist"] is True
    assert doc.worklist is not None
    pr_class = next(c for c in doc.worklist if c.kind == "pr")
    assert pr_class.total >= hand_count_red()
    # the fixture home's record names a lane on one pull request, so it is the one owner the list may show
    assert pr_class.total == pr_class.unowned + pr_class.owned
    assert pr_class.owned <= 2 < pr_class.unowned
    red_cause = next(c for c in doc.causes if c.id == "prs:red")
    assert red_cause.size == hand_count_red()
    assert doc.red_rule.violations == ()


def test_error_a_controller_state_that_cannot_be_read_makes_the_list_unread_not_empty(
    tmp_path: Path,
) -> None:
    proj = projection(declared_tickets())
    home, env = cycle_home(tmp_path, proj, state=False)
    run(home, env=env)
    doc = written(home)
    assert doc.sources_read["worklist"] is False
    assert doc.worklist is None
    assert "source:worklist" in {c.id for c in doc.causes}


def test_error_without_the_projection_the_list_is_unread(tmp_path: Path) -> None:
    home, env = cycle_home(tmp_path, None, state=True)
    run(home, env=env)
    doc = written(home)
    assert doc.sources_read["worklist"] is False
    assert doc.worklist is None


def test_error_an_unreadable_state_file_is_none(tmp_path: Path) -> None:
    bad = tmp_path / "state.json"
    bad.write_text("not json", encoding="utf-8")
    assert read_controller_owners(bad) is None
    assert read_controller_owners(tmp_path / "absent.json") is None
