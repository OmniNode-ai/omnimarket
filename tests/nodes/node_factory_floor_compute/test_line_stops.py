# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22370 (lean line board plan T5): stage counts and the thirteen stop kinds, each stop with its evidence.

Every fixture is a mixed line: one task proven done (merged, running on dev, integration-proven) and one task carrying
the stop. A stop never moves the stopped task past what its evidence proves, and never lowers the proven one.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorConfig,
    ModelFloorContext,
    ModelFloorLedgerRow,
    ModelFloorManifest,
    ModelFloorManifestTask,
    ModelFloorPlanInput,
    ModelFloorPr,
    ModelFloorRequiredContexts,
    ModelFloorSelection,
    ModelFloorSourceHealth,
)
from omnimarket.models.model_floor_line_select import (
    ModelFloorProcessDecl,
)
from omnimarket.models.model_floor_line_stops import ModelEvidenceRef, ModelLineStop
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFloorLine,
)
from tests.nodes.node_factory_floor_compute.support import (
    CONTRACT,
    NOW,
    ago,
    config,
    merge,
    pr,
    proven,
    request,
    row,
    running_on_dev,
)

pytestmark = pytest.mark.unit

PLAN = "beta/plans/2026-11-01-one-runtime-plan.md"
SOURCES = (
    "plans_clone",
    "pr_watcher",
    "ledger_window",
    "plans_today",
    "linear_snapshot",
    "running_evidence",
    "required_contexts",
    "goal_file",
)
ALL_CLASSES: Any = ("declared", "plan", "goal", "ruling", "ticket", "process")


def healthy(**states: Any) -> tuple[ModelFloorSourceHealth, ...]:
    """Every source the board depends on reads ``read`` unless the call names another state for it."""
    return tuple(
        ModelFloorSourceHealth(
            source=source,
            state=states.get(source, "read"),
            read_at=None if states.get(source) == "unread" else ago(1),
            cause="not readable" if states.get(source) == "unread" else "",
        )
        for source in SOURCES
    )


def ctx(name: str, result: str = "failure") -> ModelFloorContext:
    return ModelFloorContext(name=name, result=result)


def open_pr(key: str, *contexts: ModelFloorContext, head: str = "") -> ModelFloorPr:
    return ModelFloorPr(
        key=key,
        state="OPEN",
        red=any(c.result == "failure" for c in contexts),
        head_sha=head,
        contexts=contexts,
        tickets=("OMN-3",),
    )


def plan_two(b_prs: tuple[str, ...] = ()) -> ModelFloorPlanInput:
    """Task A is proven done through repo#1; task B (OMN-3) is the one the fixtures stop."""
    return ModelFloorPlanInput(
        plan_path=PLAN,
        title="One runtime",
        tickets=("OMN-50",),
        manifest=ModelFloorManifest(
            tasks=(
                ModelFloorManifestTask(
                    id="A", title="A", tickets=("OMN-1",), prs=("repo#1",), scoped=True
                ),
                ModelFloorManifestTask(
                    id="B", title="B", tickets=("OMN-3",), prs=b_prs, scoped=True
                ),
            )
        ),
    )


def compute(
    *,
    b_prs: tuple[str, ...] = (),
    prs: tuple[ModelFloorPr, ...] = (),
    merges: tuple[Any, ...] = (),
    ledger: tuple[ModelFloorLedgerRow, ...] = (),
    running: tuple[str, ...] = ("repo#1",),
    health: tuple[ModelFloorSourceHealth, ...] | None = None,
    classes: bool = False,
    **extra: Any,
) -> Any:
    built = request(
        plans=(plan_two(b_prs),),
        prs=(pr("repo#1", "MERGED"), *prs),
        merges=(merge("repo#1", 30), *merges),
        ledger=(*proven("repo#1"), *ledger),
        running_evidence=running_on_dev(*running),
        source_health=healthy() if health is None else health,
        **extra,
    )
    if classes:
        built = built.model_copy(
            update={"selection": ModelFloorSelection(line_classes=ALL_CLASSES)}
        )
    return compute_factory_floor(built)


def line_of(result: Any, line_id: str | None = None) -> ModelFloorLine:
    return next(
        line
        for line in result.lines
        if line.line_id == (line_id or "2026-11-01-one-runtime-plan")
    )


def kinds(line: ModelFloorLine) -> list[str]:
    return [stop.kind for stop in line.stops]


def stage_counts(line: ModelFloorLine) -> dict[str, int]:
    return {entry.stage: entry.count for entry in line.stage_counts}


def assert_no_unproven_progress(line: ModelFloorLine, merged: int = 1) -> None:
    """Only task A is proven. Whatever the stop, the line counts one task built and shows nothing running beyond it."""
    assert line.condition == "stopped"
    assert line.progress.completed == 1
    assert line.progress.total == 2
    assert line.line_evidence.merged == merged
    assert line.line_evidence.running_on_dev == 1
    assert stage_counts(line)["running"] <= 1
    assert all(stop.evidence for stop in line.stops)


# --- each stop kind: a mixed line that carries it, and the same line without the cause ---------------------------

RELEASE_ROW = row(
    "RELEASE",
    600,
    "orchestrator",
    prs=("repo#5",),
    tickets=("OMN-3",),
    head="aaaaaaaaaaaa",
)
KINDS: dict[str, dict[str, Any]] = {
    "unread": {"health": healthy(pr_watcher="unread")},
    "andon": {"b_prs": ("repo#5",), "prs": (open_pr("repo#5", ctx("CI Summary")),)},
    "lane-ended-blocked": {
        "ledger": (
            row(
                "TERMINAL",
                300,
                "lane-x",
                tickets=("OMN-3",),
                outcome="blocked",
                reason="premise failed",
            ),
        )
    },
    "lane-ended-partial": {
        "ledger": (
            row(
                "TERMINAL",
                300,
                "lane-x",
                tickets=("OMN-3",),
                outcome="partial",
                reason="half wired",
            ),
        )
    },
    "handed-off-unmerged": {
        "b_prs": ("repo#5",),
        "prs": (open_pr("repo#5"),),
        "ledger": (
            row(
                "TERMINAL",
                300,
                "lane-x",
                tickets=("OMN-3",),
                prs=("repo#5",),
                outcome="handed-off",
            ),
        ),
    },
    "merged-not-running": {
        "b_prs": ("repo#6",),
        "prs": (pr("repo#6", "MERGED", tickets=("OMN-3",)),),
        "merges": (merge("repo#6", 40, tickets=("OMN-3",)),),
    },
    "held-with-real-red": {
        "b_prs": ("repo#5",),
        "prs": (
            open_pr("repo#5", ctx("Hostile Review Gate"), ctx("CI Summary"), head="b"),
        ),
        "held_gates": ("Hostile Review Gate",),
        "required_contexts": (
            ModelFloorRequiredContexts(repo="repo", contexts=("CI Summary",)),
        ),
    },
    "release-at-old-head": {
        "b_prs": ("repo#5",),
        "prs": (open_pr("repo#5", head="bbbbbbbbbbbb2"),),
        "release_required_repos": ("repo",),
        "ledger": (RELEASE_ROW,),
    },
    "blocked-on-person": {
        "roles": ("operator",),
        "ledger": (
            row(
                "TERMINAL",
                300,
                "lane-x",
                tickets=("OMN-3",),
                outcome="blocked",
                reason="needs the operator to decide",
            ),
            row("MSG", 290, "lane-x", tickets=("OMN-3",), to="operator", row_id="m-1"),
        ),
    },
    "person-blocker-unverified": {
        "roles": ("operator",),
        "ledger": (
            row(
                "TERMINAL",
                300,
                "lane-x",
                tickets=("OMN-3",),
                outcome="blocked",
                reason="needs the operator to decide",
            ),
        ),
    },
    "ruled-not-started": {
        "classes": True,
        "ledger": (
            row("RULING", 600, "orchestrator", tickets=("OMN-3",), row_id="r-1"),
        ),
    },
}


def test_the_mixed_line_without_a_cause_has_no_stop() -> None:
    assert line_of(compute()).stops == ()


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_a_stop_kind_is_built_with_evidence_and_moves_no_task_past_its_proof(
    kind: str,
) -> None:
    line = line_of(compute(**KINDS[kind]))
    assert kind in kinds(line)
    stop = next(s for s in line.stops if s.kind == kind)
    assert stop.sentence
    assert stop.evidence
    # A merged task that is not running is read as merged (line_evidence.merged), and counted running nowhere.
    assert_no_unproven_progress(line, merged=2 if kind == "merged-not-running" else 1)
    # The stopped task is at a stage its evidence proves: never running, never merged unless a merge is read.
    counts = stage_counts(line)
    if kind != "unread":
        assert counts["running"] == 1
    if kind != "merged-not-running":
        assert counts["merged"] == 0


def test_a_merged_task_that_is_not_running_is_counted_merged_and_never_running() -> (
    None
):
    line = line_of(compute(**KINDS["merged-not-running"]))
    counts = stage_counts(line)
    assert (counts["merged"], counts["running"]) == (1, 1)
    stop = next(s for s in line.stops if s.kind == "merged-not-running")
    assert "repo#6" in stop.sentence
    assert {e.kind for e in stop.evidence} >= {"merge", "running"}


def test_a_task_with_one_pull_request_merged_and_one_open_is_pr_open() -> None:
    line = line_of(
        compute(
            b_prs=("repo#5", "repo#6"),
            prs=(open_pr("repo#5"), pr("repo#6", "MERGED")),
            merges=(merge("repo#6", 40),),
        )
    )
    counts = stage_counts(line)
    assert (counts["pr-open"], counts["merged"], counts["running"]) == (1, 0, 1)


def test_a_task_with_two_merges_of_which_one_runs_is_merged_not_running() -> None:
    line = line_of(
        compute(
            b_prs=("repo#5", "repo#6"),
            prs=(pr("repo#5", "MERGED"), pr("repo#6", "MERGED")),
            merges=(merge("repo#5", 50), merge("repo#6", 40)),
            running=("repo#1", "repo#5"),
        )
    )
    counts = stage_counts(line)
    assert (counts["merged"], counts["running"]) == (1, 1)
    assert "merged-not-running" in kinds(line)


def test_a_pull_request_closed_unmerged_proves_no_stage() -> None:
    line = line_of(
        compute(b_prs=("repo#5",), prs=(ModelFloorPr(key="repo#5", state="CLOSED"),))
    )
    counts = stage_counts(line)
    assert (counts["pr-open"], counts["merged"], counts["running"]) == (0, 0, 1)
    assert counts["planned"] == 1


def test_kinds_with_a_control_are_gone_when_the_cause_is_gone() -> None:
    gate_only = dict(KINDS["held-with-real-red"])
    gate_only["prs"] = (open_pr("repo#5", ctx("Hostile Review Gate"), head="b"),)
    assert "held-with-real-red" not in kinds(line_of(compute(**gate_only)))
    same_head = dict(KINDS["release-at-old-head"])
    same_head["prs"] = (open_pr("repo#5", head="aaaaaaaaaaaa9"),)
    assert "release-at-old-head" not in kinds(line_of(compute(**same_head)))
    merged_work = dict(KINDS["lane-ended-blocked"])
    assert "lane-ended-blocked" in kinds(line_of(compute(**merged_work)))


def idle_line(minutes_ago: float, **extra: Any) -> ModelFloorLine:
    """A line whose only task has no pull request and whose only ledger row is ``minutes_ago`` old."""
    plan = ModelFloorPlanInput(
        plan_path=PLAN,
        title="One runtime",
        tickets=("OMN-50",),
        manifest=ModelFloorManifest(
            tasks=(
                ModelFloorManifestTask(
                    id="B", title="B", tickets=("OMN-3",), scoped=True
                ),
            )
        ),
    )
    built = request(
        plans=(plan,),
        ledger=(row("STATUS", minutes_ago, "lane-x", tickets=("OMN-3",)),),
        source_health=healthy(),
        **extra,
    )
    return line_of(compute_factory_floor(built))


def test_a_line_nothing_moved_on_in_a_day_with_no_owner_is_idle() -> None:
    line = idle_line(60 * 30)
    assert kinds(line) == ["idle"]
    assert "nothing moved in 30 h" in line.stops[0].sentence
    assert line.condition == "stopped"
    assert stage_counts(line)["running"] == 0


def test_a_line_that_moved_today_is_not_idle() -> None:
    assert idle_line(60).stops == ()


def test_a_line_with_a_live_owner_is_not_idle() -> None:
    claim = row("CLAIM", 5, "lane-y", tickets=("OMN-3",))
    plan_line = idle_line(60 * 30, running_evidence=())
    assert kinds(plan_line) == ["idle"]
    with_owner = line_of(
        compute_factory_floor(
            request(
                plans=(
                    ModelFloorPlanInput(
                        plan_path=PLAN,
                        title="One runtime",
                        tickets=("OMN-50",),
                        manifest=ModelFloorManifest(
                            tasks=(
                                ModelFloorManifestTask(
                                    id="B", title="B", tickets=("OMN-3",), scoped=True
                                ),
                            )
                        ),
                    ),
                ),
                ledger=(row("STATUS", 60 * 30, "lane-x", tickets=("OMN-3",)), claim),
                source_health=healthy(),
            )
        )
    )
    assert with_owner.owner == "lane-y"
    assert "idle" not in kinds(with_owner)


def test_stops_print_in_the_contracts_order() -> None:
    result = compute(
        **{
            **KINDS["lane-ended-partial"],
            "health": healthy(running_evidence="unread"),
        }
    )
    order = [r.kind for r in config().line_stops.stop_kinds]
    shown = kinds(line_of(result))
    assert shown == sorted(shown, key=order.index)
    assert shown[0] == "unread"


# --- the process line --------------------------------------------------------------------------------------------


def idle_run(minutes_ago: float, run: str, **cells: Any) -> ModelFloorLedgerRow:
    return ModelFloorLedgerRow(
        ts=ago(minutes_ago),
        kind="FRICTION",
        lane="lab-fill",
        actor="launchd:lab-fill",
        run_id=run,
        cells=(("class", "lab-idle"), ("free-slots", "19"), ("run", run)),
        **cells,
    )


FILL = ModelFloorProcessDecl(
    name="lab-fill",
    actor="launchd:lab-fill",
    run_kind="FRICTION",
    run_match="actor=launchd:lab-fill",
    did_nothing_match="class=lab-idle;free-slots>0",
    interval_min=20,
    outcomes_covered=("did-nothing",),
)


def fill_line(*rows: ModelFloorLedgerRow) -> ModelFloorLine:
    result = compute(classes=True, ledger=rows, processes=(FILL,), health=healthy())
    return line_of(result, "process:lab-fill")


def test_three_consecutive_idle_runs_are_a_process_stop_with_a_missing_cause() -> None:
    line = fill_line(
        idle_run(101, "2026-11-08T1810Z"),
        idle_run(81, "2026-11-08T1830Z"),
        idle_run(61, "2026-11-08T1850Z"),
    )
    assert kinds(line) == ["process-did-nothing"]
    stop = line.stops[0]
    assert "3 runs did nothing" in stop.sentence
    assert len(stop.evidence) == 3
    assert stop.cause == config().line_stops.cause_missing
    assert line.condition == "stopped"


@pytest.mark.parametrize(
    "runs",
    [
        (idle_run(81, "2026-11-08T1830Z"), idle_run(61, "2026-11-08T1850Z")),
        (
            idle_run(121, "2026-11-08T1810Z"),
            idle_run(81, "2026-11-08T1830Z"),
            idle_run(41, "2026-11-08T1910Z"),
        ),
    ],
    ids=["two-runs", "a-gap-between-runs"],
)
def test_fewer_than_three_consecutive_idle_runs_is_no_process_stop(
    runs: tuple[ModelFloorLedgerRow, ...],
) -> None:
    assert "process-did-nothing" not in kinds(fill_line(*runs))


def test_a_run_that_did_something_ends_the_chain() -> None:
    summary = ModelFloorLedgerRow(
        ts=ago(41),
        kind="STATUS",
        lane="lab-fill",
        actor="launchd:lab-fill",
        run_id="2026-11-08T1910Z",
    )
    line = fill_line(
        idle_run(101, "2026-11-08T1810Z"),
        idle_run(81, "2026-11-08T1830Z"),
        idle_run(61, "2026-11-08T1850Z"),
        summary,
    )
    assert "process-did-nothing" not in kinds(line)


# --- AC3: an unread source gives unread, never a zero, fine or a missing stop -------------------------------------


@pytest.mark.parametrize(
    ("state", "unread_stop"),
    [("read", False), ("read-empty", False), ("stale", True), ("unread", True)],
)
def test_each_source_health_state_gives_its_own_answer(
    state: str, unread_stop: bool
) -> None:
    line = line_of(compute(health=healthy(pr_watcher=state)))
    assert ("unread" in kinds(line)) is unread_stop
    if unread_stop:
        stop = next(s for s in line.stops if s.kind == "unread")
        assert stop.sentence.startswith("UNREAD: pr_watcher")
        assert stop.evidence[0].kind == "source-health"
        assert line.condition == "stopped"
    else:
        assert line.stops == ()


def test_an_unread_watcher_counts_no_task_as_progressing() -> None:
    line = line_of(compute(health=healthy(pr_watcher="unread"), b_prs=("repo#5",)))
    counts = stage_counts(line)
    assert counts["unread"] == 2
    assert (counts["pr-open"], counts["merged"], counts["running"]) == (0, 0, 0)


# --- AC4: the person-blocker pair --------------------------------------------------------------------------------


def person(*extra: ModelFloorLedgerRow, role_ticket: str = "OMN-3") -> list[str]:
    blocker = row(
        "TERMINAL",
        300,
        "lane-x",
        tickets=("OMN-3",),
        outcome="blocked",
        reason="needs the operator to decide",
    )
    result = compute(roles=("operator",), ledger=(blocker, *extra))
    return [
        k for k in kinds(line_of(result)) if k.startswith(("blocked-on", "person-"))
    ]


def test_an_unanswered_ask_on_the_same_subject_is_blocked_on_a_person() -> None:
    ask = row("MSG", 290, "lane-x", tickets=("OMN-3",), to="operator", row_id="m-1")
    assert person(ask) == ["blocked-on-person"]


def test_an_ask_on_another_subject_is_an_unverified_blocker() -> None:
    ask = row("MSG", 290, "lane-x", tickets=("OMN-99",), to="operator", row_id="m-1")
    assert person(ask) == ["person-blocker-unverified"]


def test_an_answered_ask_is_an_unverified_blocker() -> None:
    ask = row("MSG", 290, "lane-x", tickets=("OMN-3",), to="operator", row_id="m-1")
    answer = row("ACK", 280, "operator", tickets=("OMN-3",), re="m-1")
    assert person(ask, answer) == ["person-blocker-unverified"]


def test_no_ask_is_an_unverified_blocker() -> None:
    assert person() == ["person-blocker-unverified"]


# --- AC5: a stop cannot be built without evidence; a stop without a cause row says so -----------------------------


def test_a_stop_with_an_empty_evidence_tuple_cannot_be_built() -> None:
    with pytest.raises(ValidationError):
        ModelLineStop(kind="idle", sentence="nothing moved", evidence=())
    ModelLineStop(
        kind="idle",
        sentence="nothing moved",
        evidence=(
            ModelEvidenceRef(
                kind="ledger-row", ref="STATUS 2026-11-08T00:00:00Z lane=x"
            ),
        ),
    )


def test_a_stop_without_a_cause_row_says_so_at_any_age() -> None:
    cause = config().line_stops.cause_missing
    for minutes in (30, 101, 60 * 40):
        line = line_of(
            compute(
                ledger=(
                    row(
                        "TERMINAL",
                        minutes,
                        "lane-x",
                        tickets=("OMN-3",),
                        outcome="blocked",
                        reason="x",
                    ),
                )
            )
        )
        assert [s.cause for s in line.stops if s.kind == "lane-ended-blocked"] == [
            cause
        ]


def test_a_cause_row_names_the_cause() -> None:
    line = line_of(
        compute(
            **{
                **KINDS["lane-ended-blocked"],
                "ledger": (
                    *KINDS["lane-ended-blocked"]["ledger"],
                    ModelFloorLedgerRow(
                        ts=ago(100),
                        kind="CORRECTION",
                        lane="orchestrator",
                        tickets=("OMN-3",),
                        cells=(("cause", "the gate was never wired"),),
                    ),
                ),
            }
        )
    )
    stop = next(s for s in line.stops if s.kind == "lane-ended-blocked")
    assert "the gate was never wired" in stop.cause
    assert "CORRECTION" in stop.cause


# --- AC6: every value is contract config -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", ["stop_kinds", "stage_order", "unread_sources", "cause_missing"]
)
def test_deleting_a_line_stops_key_fails_closed_naming_it(key: str) -> None:
    raw = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["config"]
    del raw["line_stops"][key]
    with pytest.raises(ValidationError, match=key):
        ModelFactoryFloorConfig.model_validate(raw)


def test_deleting_the_whole_block_fails_closed_naming_it() -> None:
    raw = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["config"]
    del raw["line_stops"]
    with pytest.raises(ValidationError, match="line_stops"):
        ModelFactoryFloorConfig.model_validate(raw)


def test_the_contract_declares_the_thirteen_kinds_in_the_plans_order() -> None:
    declared = [r.kind for r in config().line_stops.stop_kinds]
    assert declared == [
        "unread",
        "andon",
        "lane-ended-blocked",
        "lane-ended-partial",
        "handed-off-unmerged",
        "merged-not-running",
        "held-with-real-red",
        "release-at-old-head",
        "blocked-on-person",
        "person-blocker-unverified",
        "ruled-not-started",
        "process-did-nothing",
        "idle",
    ]
    assert set(KINDS) | {"process-did-nothing", "idle"} == set(declared)


def test_a_sentence_comes_from_the_contract_not_the_code() -> None:
    cfg = config()
    changed = cfg.line_stops.model_copy(
        update={
            "stop_kinds": tuple(
                r.model_copy(update={"sentence": "IDLE {hours}"})
                if r.kind == "idle"
                else r
                for r in cfg.line_stops.stop_kinds
            )
        }
    )
    built = request(
        plans=(plan_two(),),
        ledger=(row("STATUS", 60 * 30, "lane-x", tickets=("OMN-3",)),),
        source_health=healthy(),
        cfg=cfg.model_copy(update={"line_stops": changed}),
    )
    stop = next(
        s for s in line_of(compute_factory_floor(built)).stops if s.kind == "idle"
    )
    assert stop.sentence == "IDLE 30"
    assert NOW  # the clock is the request's, not the wall
