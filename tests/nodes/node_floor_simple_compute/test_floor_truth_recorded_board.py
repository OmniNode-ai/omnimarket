# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor board told the truth where it was checked against live reads (OMN-22478, lane floor-truth-0250).

The inputs are the reads of the board cycle recorded at 2026-11-06T02:54:20Z (floor-simple-reads.json and the record the
page served), with each lane's pull request from the record and each class's pull requests from the watcher state. Each
test names the false reading it came from and fails on the code that drew it:

- a lane that only shares the landing ticket was shown as working on five unrelated causes;
- a stoppage was drawn without how old it is, from a rollup built hours earlier, with an estimate shown as lane-minutes lost;
- joins into pieces that do not exist were judged against two deliberate drills and reported as broken chains;
- a piece whose runners are unread was drawn green;
- a conflicted pull request was called green and waiting.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorCause,
    ModelFloorLane,
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelFloorSimpleOverlay,
    ModelGoalScope,
    ModelLabStateOutput,
    ModelLabStateOverlay,
    ModelNodeEmission,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
    check_red_rule,
    live_lanes,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    read_queue_age,
    read_watcher,
    record_stoppages,
)
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "recorded_board_0254.json"
ROLLUP_BUILT = datetime(2026, 11, 6, 1, 7, 5, tzinfo=UTC)


def recorded() -> dict[str, Any]:
    loaded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def board(
    *, lab_overrides: dict[str, str] | None = None
) -> tuple[ModelFloorSimple, ModelFloorSimpleInput]:
    data = recorded()
    raw = dict(data["floor_input"])
    lab = raw["lab"]
    for verdict in lab["verdicts"]:
        if lab_overrides and verdict["id"] in lab_overrides:
            verdict["state"] = lab_overrides[verdict["id"]]
    now = datetime.fromisoformat(raw["now"])
    raw.update(
        {
            "overlay": ModelFloorSimpleOverlay.model_validate(
                overlay_text("node_floor_simple_compute")
            ),
            "lanes": tuple(ModelFloorLane.model_validate(x) for x in data["lanes"]),
            "pr_class_prs": {k: tuple(v) for k, v in data["pr_class_prs"].items()},
            "friction_as_of": ROLLUP_BUILT,
            "stoppages": record_stoppages(
                {"floor": {"stoppages": data["stoppages_raw"]}}
            ),
            "lab": ModelLabStateOutput.model_validate(lab),
            "now": now,
        }
    )
    request = ModelFloorSimpleInput.model_validate(raw)
    return HandlerFloorSimple().handle(request), request


def cause(doc: ModelFloorSimple, cause_id: str) -> ModelFloorCause:
    return next(c for c in doc.causes if c.id == cause_id)


# --- work under way comes from a lane really on the cause -------------------------------------------------------------


def test_golden_the_recorded_board_has_lanes_sharing_the_landing_ticket_and_none_of_them_own_its_causes() -> (
    None
):
    """The premise: many lanes carry the landing ticket, so a ticket match would name all of them on every cause."""
    _, request = board()
    shared = [
        lane.lane
        for lane in request.lanes
        if lane.ticket == "OMN-19998" and not lane.stalled
    ]
    assert len(shared) >= 10


@pytest.mark.parametrize(
    "cause_id",
    [
        "stoppage:unclassified",
        "stoppage:throughput-results",
        "stoppage:job-exit",
    ],
)
def test_error_a_stoppage_is_not_work_under_way_because_lanes_share_its_ticket(
    cause_id: str,
) -> None:
    doc, _ = board()
    found = cause(doc, cause_id)
    assert found.lanes == ()
    assert (found.state, found.work_under_way) == ("red", False)


def test_golden_a_pull_request_class_names_only_lanes_on_its_pull_requests() -> None:
    doc, request = board()
    red = cause(doc, "prs:red")
    on_red = {
        lane.lane
        for lane in request.lanes
        if not lane.stalled and lane.pr in request.pr_class_prs["red"]
    }
    assert on_red, "the recorded board has landing workers on red pull requests"
    assert set(red.lanes) == on_red
    assert (red.state, red.work_under_way) == ("amber", True)
    shared_only = {
        lane.lane
        for lane in request.lanes
        if lane.ticket == "OMN-19998" and lane.lane not in on_red
    }
    assert shared_only, "lanes with the landing ticket and no red pull request exist"
    assert not shared_only & set(red.lanes)


def test_error_a_lane_with_only_the_shared_ticket_is_not_on_a_pull_request_class() -> (
    None
):
    lanes = (ModelFloorLane(lane="x", ticket="OMN-19998", pr=""),)
    assert live_lanes(lanes, owners=(), prs=("repo#1",)) == ()
    on = (ModelFloorLane(lane="y", ticket="OMN-19998", pr="repo#1"),)
    assert live_lanes(on, owners=(), prs=("repo#1",)) == ("y",)
    stalled = (ModelFloorLane(lane="z", pr="repo#1", stalled=True),)
    assert live_lanes(stalled, owners=(), prs=("repo#1",)) == ()


def test_golden_a_declared_owner_ticket_still_counts() -> None:
    lanes = (ModelFloorLane(lane="o", ticket="OMN-7"),)
    assert live_lanes(lanes, owners=("OMN-7",), prs=()) == ("o",)


def test_golden_the_colour_rule_holds_on_the_recorded_board() -> None:
    doc, request = board()
    assert doc.red_rule.state == "WORKING"
    assert check_red_rule(doc, request.lanes).violations == ()


# --- a stoppage carries its age, whether it is still occurring, and no estimate shown as lost time -------------------------


def test_golden_a_stoppage_seen_recently_is_still_occurring_with_its_age() -> None:
    doc, _ = board()
    found = cause(doc, "stoppage:hook-refusal:unauthorised-shared-tree-git-mutation")
    assert found.still_occurring is True
    assert found.last_seen_age_s is not None
    assert found.last_seen_age_s < 3 * 3600
    assert "last seen" in found.detail
    assert "still occurring" in found.detail


def test_error_a_stoppage_not_seen_for_hours_says_so_instead_of_reading_like_a_live_problem() -> (
    None
):
    doc, _ = board()
    found = cause(doc, "stoppage:lab-enumerate-partial")
    assert found.still_occurring is False
    assert found.last_seen_age_s is not None
    assert found.last_seen_age_s > 6 * 3600
    assert "no row since in the rollup" in found.detail
    # It stays listed and red: nobody is on it. Only its description became true.
    assert (found.state, found.listed) == ("red", True)


def test_golden_the_row_names_the_rollup_it_was_read_from() -> None:
    doc, _ = board()
    found = cause(doc, "stoppage:lab-idle")
    assert "From the friction rollup built" in found.detail
    assert "later rows are not in it" in found.detail


def test_error_an_estimated_cost_is_not_shown_as_lane_minutes_lost() -> None:
    doc, _ = board()
    idle = cause(doc, "stoppage:lab-idle")
    assert "20540" not in idle.detail
    assert "lost" not in idle.detail
    assert "no measured cost" in idle.detail
    assert all(
        "lost" not in c.detail for c in doc.causes if c.id.startswith("stoppage:")
    )
    # A figure the overlay does not mark as an estimate is shown as the reporter's own count.
    hook = cause(doc, "stoppage:hook-refusal:unauthorised-shared-tree-git-mutation")
    assert "73 lane-minutes logged" in hook.detail


def test_error_with_no_overlay_window_a_stoppage_is_not_judged_either_way() -> None:
    _, request = board()
    causes = request.overlay.causes.model_copy(
        update={"still_occurring_within_s": None}
    )
    overlay = request.overlay.model_copy(update={"causes": causes})
    doc = HandlerFloorSimple().handle(request.model_copy(update={"overlay": overlay}))
    assert cause(doc, "stoppage:lab-idle").still_occurring is None


# --- the lab sources and the pieces that stand on them -----------------------------------------------------------------


def test_error_unread_is_named_unread_and_is_not_counted_as_not_working() -> None:
    doc, request = board()
    states = [v.state for v in request.lab.verdicts] if request.lab else []
    unread = states.count("UNKNOWN")
    assert unread >= 1, "the recorded board has the runner groups unread"
    title = cause(doc, "lab:state").title
    assert f"{unread} not read" in title
    assert "are not working" not in title


def test_error_piece_8_does_not_draw_green_while_its_runner_groups_are_unread() -> None:
    doc, _ = board()
    piece = next(p for p in doc.pieces if p.piece == 8)
    assert piece.status != "working"
    assert piece.draw in ("red", "amber")
    assert piece.cause_id == "lab:runner_groups"
    assert "CI runner groups not read" in piece.text
    # The cause is the lab source's own, not a second cause for the piece.
    assert "piece:8" not in {c.id for c in doc.causes}


def test_golden_piece_8_is_green_once_the_runner_groups_are_read() -> None:
    doc, _ = board(lab_overrides={"runner_groups": "WORKING"})
    piece = next(p for p in doc.pieces if p.piece == 8)
    assert (piece.status, piece.draw) == ("working", "green")


# --- the chains: a join into a piece that does not exist is not judged, and a drill is not evidence for a join ------------


def emissions() -> tuple[ModelNodeEmission, ...]:
    return tuple(ModelNodeEmission.model_validate(e) for e in recorded()["emissions"])


def receipts(
    judged: tuple[int, ...] | None, events: tuple[ModelNodeEmission, ...] | None = None
) -> ModelFloorReceipts:
    overlay = ModelFloorSimpleOverlay.model_validate(
        overlay_text("node_floor_simple_compute")
    )
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=datetime(2026, 11, 6, 2, 54, 20, tzinfo=UTC),
            emissions=events if events is not None else emissions(),
            pieces=overlay.pieces,
            joins=overlay.joins,
            freshness_max_age_s=overlay.freshness_max_age_s,
            error_max_age_s=overlay.error_receipt_max_age_s,
            chain_deadline_s=overlay.chain_deadline_s,
            judged_to_pieces=judged,
        )
    )


def test_error_two_drills_of_piece_17_are_not_broken_joins_to_pieces_1_and_18() -> None:
    drills = [e for e in emissions() if e.drill]
    assert len(drills) == 4
    assert {e.failure_code for e in drills if e.failure_code} == {
        "record_payload_invalid"
    }
    result = receipts(judged=(17,))
    assert [
        f for f in result.findings if f.code == "downstream_missing_typed_failure"
    ] == []
    # The same drills with every join judged: a drill is still not evidence for a join.
    assert receipts(judged=None).findings == ()
    joins = {j.join: j.state for j in result.joins}
    assert joins["J0"] == "not_run"
    assert joins["J17"] == "not_run"


def failing_chain(cid: str, *, drill: bool) -> tuple[ModelNodeEmission, ...]:
    base = {
        "node": "node_floor_simple_effect:read_sources",
        "piece": 17,
        "correlation_id": cid,
        "backend": "real",
        "drill": drill,
    }
    t0 = datetime(2026, 11, 6, 2, 0, tzinfo=UTC)
    return (
        ModelNodeEmission.model_validate({**base, "phase": "start", "emitted_at": t0}),
        ModelNodeEmission.model_validate(
            {
                **base,
                "phase": "failure",
                "emitted_at": t0 + timedelta(seconds=1),
                "failure_code": "record_payload_invalid",
                "failure_detail": "x",
            }
        ),
    )


def test_error_a_real_failure_still_breaks_a_join_into_a_piece_that_exists() -> None:
    """The rule still bites: judged pieces, a real failure, a silent downstream past the deadline."""
    real = failing_chain("c" * 32, drill=False)
    broken = receipts(judged=(1, 17), events=real)
    assert [f.subject for f in broken.findings] == ["J0 piece 17 to 1"]
    assert broken.findings[0].code == "downstream_missing_typed_failure"
    # Into a piece that does not exist (18 is not judged) nothing is reported.
    assert all("to 18" not in f.subject for f in broken.findings)
    # And the same failure as a drill reports nothing.
    drill = failing_chain("d" * 32, drill=True)
    assert receipts(judged=(1, 17), events=drill).findings == ()


# --- the queue age: a conflicted pull request is not green and waiting --------------------------------------------------


def watcher_state(merge_state: str) -> dict[str, Any]:
    now = datetime(2026, 11, 6, 2, 39, tzinfo=UTC)
    done = (now - timedelta(minutes=2439)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "last_tick": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prs": {
            "repo-d#1586": {
                "cls": "green-unarmed",
                "ci": {"detail": [["check", "1", done]]},
                "review": {"merge_state": merge_state},
                "facts": {
                    "state": "OPEN",
                    "draft": False,
                    "title": "fix(OMN-19998): resume landing",
                    "head_ref": "omn-19998-codex-landing-retries",
                },
            }
        },
        "merges": {},
    }


def queue_note(tmp_path: Path, merge_state: str) -> tuple[bool, str, str]:
    lab = ModelLabStateOverlay.model_validate(overlay_text("node_floor_simple_effect"))
    scope = ModelGoalScope(ruling="test", in_scope_repos=("repo-d",))
    path = tmp_path / "state.json"
    path.write_text(json.dumps(watcher_state(merge_state)), encoding="utf-8")
    now = datetime(2026, 11, 6, 2, 39, tzinfo=UTC)
    watcher = read_watcher(
        path,
        now,
        excluded_classes=("held-excluded",),
        queue_classes=lab.queue_age_classes,
        merge_window_minutes=60,
        scope=scope,
        conflict_states=lab.conflict_merge_states,
    )
    assert watcher is not None
    read = read_queue_age(watcher, lab)
    subject = read.subjects[0]
    return subject.ok, subject.note, read.summary


def test_error_a_conflicted_green_pull_request_is_not_called_green_and_waiting(
    tmp_path: Path,
) -> None:
    ok, note, summary = queue_note(tmp_path, "DIRTY")
    assert ok is False, "it is still a problem: it stays over the limit and red"
    assert "branch conflicts" in note
    assert "cannot be armed" in note
    assert "green and waiting" not in note
    assert "conflicts" in summary


def test_golden_a_clean_green_pull_request_is_still_green_and_waiting(
    tmp_path: Path,
) -> None:
    ok, note, _ = queue_note(tmp_path, "CLEAN")
    assert ok is False
    assert note.startswith("green and waiting 2439 min")


# --- a join judges the piece, not whichever node of it was read last ---------------------------------------------------


def node_events(node: str, cid: str, *, fail: bool) -> tuple[ModelNodeEmission, ...]:
    t0 = datetime(2026, 11, 6, 2, 0, tzinfo=UTC)
    base = {
        "node": node,
        "piece": 17,
        "correlation_id": cid,
        "backend": "real",
        "drill": False,
    }
    end = (
        {"phase": "failure", "failure_code": "x", "failure_detail": "x"}
        if fail
        else {"phase": "success"}
    )
    return (
        ModelNodeEmission.model_validate({**base, "phase": "start", "emitted_at": t0}),
        ModelNodeEmission.model_validate(
            {**base, **end, "emitted_at": t0 + timedelta(seconds=2)}
        ),
    )


@pytest.mark.parametrize("failing_node_last", [True, False])
def test_error_two_nodes_of_one_piece_under_one_id_do_not_replace_each_other_in_a_join(
    failing_node_last: bool,
) -> None:
    """A cycle runs several nodes of piece 17 under one id. The join must see that one of them failed, whatever the order."""
    ok = node_events("node_a:read", "e" * 32, fail=False)
    bad = node_events("node_b:alarm", "e" * 32, fail=True)
    events = (*ok, *bad) if failing_node_last else (*bad, *ok)
    result = receipts(judged=(1, 17), events=events)
    assert [f.code for f in result.findings] == ["downstream_missing_typed_failure"]
    assert result.findings[0].subject == "J0 piece 17 to 1"


def test_golden_two_succeeding_nodes_of_one_piece_make_no_finding() -> None:
    events = (
        *node_events("node_a:read", "f" * 32, fail=False),
        *node_events("node_b:alarm", "f" * 32, fail=False),
    )
    assert receipts(judged=(1, 17), events=events).findings == ()
