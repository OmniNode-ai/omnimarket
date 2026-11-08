# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Line selection by six classes (OMN-22369, lean line board plan T4): union, dedup, ids, replay and coverage."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorRequest,
    ModelFloorSelection,
    ModelFloorWatcher,
)
from omnimarket.models.model_floor_line_select import (
    ModelFloorGoalRow,
    ModelFloorProcessDecl,
    ModelFloorSourceHealth,
)
from omnimarket.nodes.node_factory_floor_compute.handlers import (
    floor_line_select,
    handler_factory_floor,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    HandlerFactoryFloor,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFactoryFloorResult,
)
from tests.nodes.node_factory_floor_compute.support import (
    NOW,
    ago,
    config,
    row,
    table_plan,
    wave_plan,
)

ALL_CLASSES = ("declared", "plan", "goal", "ruling", "ticket", "process")
DECLARED_A = "beta/plans/2026-10-31-alpha-plan.md"
DECLARED_B = "beta/plans/2026-10-31-bravo-plan.md"
PLANS_TODAY_ONLY_1 = "beta/plans/2026-11-02-charlie-plan.md"
PLANS_TODAY_ONLY_2 = "beta/plans/2026-11-02-delta-plan.md"


def _health(
    plans_today: str = "read", when_min: float = 30
) -> tuple[ModelFloorSourceHealth, ...]:
    read_at = ago(when_min)
    return tuple(
        ModelFloorSourceHealth(
            source=source,
            state=plans_today if source == "plans_today" else "read",
            read_at=read_at,
        )
        for source in (
            "plans_clone",
            "plans_today",
            "goal_file",
            "ledger",
            "pr_watcher",
            "linear_snapshot",
            "running_evidence",
        )
    )


def _nine_line_request(**overrides: object) -> ModelFactoryFloorRequest:
    """Two declared plans, three plans-today plans (one also declared), a goal row, an unmapped ruling, a live claim on a
    ticket no plan maps and two processes: nine lines."""
    plans = (
        wave_plan(DECLARED_A),
        table_plan(DECLARED_B).model_copy(update={"tickets": ("OMN-7",)}),
        table_plan(PLANS_TODAY_ONLY_1).model_copy(update={"tickets": ("OMN-71",)}),
        table_plan(PLANS_TODAY_ONLY_2).model_copy(update={"tickets": ("OMN-72",)}),
    )
    fields: dict[str, object] = {
        "generated_at": NOW,
        "config": config(),
        "selection": ModelFloorSelection(
            plan_path_contains=("alpha-plan", "bravo-plan"),
            plans_today_paths=(DECLARED_B, PLANS_TODAY_ONLY_1, PLANS_TODAY_ONLY_2),
            line_classes=ALL_CLASSES,
        ),
        "plans": plans,
        "watcher": ModelFloorWatcher(last_tick=ago(3)),
        "ledger": (
            row("CLAIM", 5, "lane-claims", tickets=("OMN-900",), row_id="c1"),
            row(
                "RULING",
                60,
                "orchestrator-31d9",
                tickets=("OMN-901",),
                row_id="2026-11-08T13:00:00Z",
                text="a ruling no plan maps",
            ),
        ),
        "goal_rows": (ModelFloorGoalRow(row_id="g1", name="a goal row"),),
        "processes": (
            ModelFloorProcessDecl(name="lab-fill"),
            ModelFloorProcessDecl(name="pr-watcher"),
        ),
        "source_health": _health(),
    }
    fields.update(overrides)
    return ModelFactoryFloorRequest(**fields)


def _compute(request: ModelFactoryFloorRequest) -> ModelFactoryFloorResult:
    return HandlerFactoryFloor().handle(request)


def _assert_nine_lines(result: ModelFactoryFloorResult) -> None:
    ids = [line.line_id for line in result.lines]
    assert len(ids) == 9, ids
    assert len(set(ids)) == 9, ids
    assert Counter(line.line_class for line in result.lines) == {
        "declared": 2,
        "plan": 2,
        "goal": 1,
        "ruling": 1,
        "ticket": 1,
        "process": 2,
    }


def test_select_yields_nine_lines_one_per_line_id_for_the_nine_line_fixture() -> None:
    result = _compute(_nine_line_request())
    _assert_nine_lines(result)
    by_id = {line.line_id: line for line in result.lines}
    # The plan chosen by both the overlay and plans-today is one line, at the lower class, with its existing id.
    assert by_id["2026-10-31-bravo-plan"].line_class == "declared"
    assert by_id["2026-10-31-alpha-plan"].line_class == "declared"
    assert by_id["2026-11-02-charlie-plan"].line_class == "plan"
    assert "goal:g1" in by_id
    assert "ruling:2026-11-08T13:00:00Z" in by_id
    assert "ticket:OMN-900" in by_id
    assert {"process:lab-fill", "process:pr-watcher"} <= set(by_id)


def test_select_orders_by_class_then_overlay_order_then_id() -> None:
    result = _compute(_nine_line_request())
    assert [line.line_id for line in result.lines] == [
        "2026-10-31-alpha-plan",
        "2026-10-31-bravo-plan",
        "2026-11-02-charlie-plan",
        "2026-11-02-delta-plan",
        "goal:g1",
        "ruling:2026-11-08T13:00:00Z",
        "ticket:OMN-900",
        "process:lab-fill",
        "process:pr-watcher",
    ]


def test_select_a_mutant_that_keeps_duplicates_fails_the_nine_line_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_nine_lines(_compute(_nine_line_request()))

    def keeps_duplicates(
        request: ModelFactoryFloorRequest,
        picks: tuple[floor_line_select.PlanPick, ...],
    ) -> tuple[floor_line_select.PlanPick, ...]:
        return tuple(
            sorted(
                picks,
                key=lambda p: (
                    floor_line_select.class_rank(request, p.line_class),
                    p.position,
                    floor_line_select.line_id_of(p.plan),
                ),
            )
        )

    monkeypatch.setattr(floor_line_select, "lowest_class_once", keeps_duplicates)
    with pytest.raises(AssertionError):
        _assert_nine_lines(_compute(_nine_line_request()))


def test_select_a_ruling_that_maps_to_a_plan_line_adds_its_id_to_that_line_and_is_no_line() -> (
    None
):
    mapped = row(
        "RULING",
        60,
        "orchestrator-31d9",
        tickets=("OMN-100",),
        row_id="r-maps",
        text="rules on the alpha plan",
    )
    base = _nine_line_request()
    result = _compute(base.model_copy(update={"ledger": (*base.ledger, mapped)}))
    ids = [line.line_id for line in result.lines]
    assert "ruling:r-maps" not in ids
    assert len(ids) == 9
    alpha = next(line for line in result.lines if line.line_id.endswith("alpha-plan"))
    assert alpha.ruled_by == ("r-maps",)


def test_select_a_ticket_a_plan_maps_is_no_ticket_line() -> None:
    claim = row("CLAIM", 5, "lane-plan", tickets=("OMN-100",), row_id="c-plan")
    base = _nine_line_request()
    result = _compute(base.model_copy(update={"ledger": (*base.ledger, claim)}))
    assert "ticket:OMN-100" not in {line.line_id for line in result.lines}


def test_select_a_ruling_older_than_the_window_is_no_line() -> None:
    old = row(
        "RULING",
        timedelta(days=15).total_seconds() / 60,
        "orchestrator-31d9",
        tickets=("OMN-902",),
        row_id="r-old",
    )
    base = _nine_line_request()
    result = _compute(base.model_copy(update={"ledger": (*base.ledger, old)}))
    assert "ruling:r-old" not in {line.line_id for line in result.lines}


def test_select_a_plan_plans_today_names_with_no_plan_supplied_is_drawn_stopped_not_dropped() -> (
    None
):
    base = _nine_line_request()
    missing = "beta/plans/2026-11-02-echo-plan.md"
    result = _compute(
        base.model_copy(
            update={
                "selection": base.selection.model_copy(
                    update={
                        "plans_today_paths": (
                            *(base.selection.plans_today_paths or ()),
                            missing,
                        )
                    }
                )
            }
        )
    )
    echo = next(line for line in result.lines if line.line_id == "2026-11-02-echo-plan")
    assert echo.line_class == "plan"
    assert echo.source == "undrawn"
    assert echo.condition == "stopped"


def test_select_a_run_that_names_only_the_floors_own_classes_draws_no_other_line() -> (
    None
):
    base = _nine_line_request()
    result = _compute(
        base.model_copy(
            update={
                "selection": base.selection.model_copy(
                    update={"line_classes": ("declared", "plan")}
                )
            }
        )
    )
    assert {line.line_class for line in result.lines} == {"declared", "plan"}
    assert [c.line_class for c in result.coverage] == ["declared", "plan"]


# ------------------------------------------------------------------------------- existing line ids (T4-B)

# The six declared lines of the published factory floor record (its generated_at 2026-11-08T14:01:28Z), the overlay's six
# plan_path_contains texts and the ids their plans have always drawn. The record of 2026-11-03T13:21:32Z itself is
# not served any more, so this list is the nearest record that could be read.
RECORDED_LINE_IDS = (
    "2026-10-01-claude-code-context-and-delegation-adapter-plan",
    "2026-10-27-compaction-mod-measurement-plan",
    "2026-10-28-validators-to-core-and-hardcoded-rules",
    "2026-10-31-orchestration-alignment-wave-plan",
    "2026-11-01-one-runtime-plan",
    "2026-10-31-view-centric-interfaces",
)
RECORDED_TEXTS = (
    "claude-code-context-and-delegation-adapter",
    "compaction-mod-measurement",
    "validators-to-core-and-hardcoded-rules",
    "orchestration-alignment-wave",
    "one-runtime",
    "view-centric-interfaces",
)


def _recorded_plans_request(**selection: object) -> ModelFactoryFloorRequest:
    plans = tuple(
        wave_plan(f"beta/plans/{plan_id}.md") for plan_id in RECORDED_LINE_IDS
    )
    return ModelFactoryFloorRequest(
        generated_at=NOW,
        config=config(),
        selection=ModelFloorSelection(plan_path_contains=RECORDED_TEXTS, **selection),
        plans=plans,
        watcher=ModelFloorWatcher(last_tick=ago(3)),
    )


def test_line_class_declared_ids_equal_the_recorded_six() -> None:
    result = _compute(_recorded_plans_request())
    assert tuple(line.line_id for line in result.lines) == tuple(
        sorted(RECORDED_LINE_IDS, key=RECORDED_LINE_IDS.index)
    )
    assert {line.line_class for line in result.lines} == {"declared"}


def test_line_class_the_ids_do_not_change_when_every_class_is_drawn() -> None:
    result = _compute(
        _recorded_plans_request(
            plans_today_paths=tuple(f"beta/plans/{i}.md" for i in RECORDED_LINE_IDS),
            line_classes=ALL_CLASSES,
        )
    )
    assert tuple(line.line_id for line in result.lines) == RECORDED_LINE_IDS


def test_line_class_a_mutant_that_renames_a_declared_id_fails_the_recorded_six(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handler_factory_floor,
        "_line_id",
        lambda plan: "x-" + plan.plan_path.rsplit("/", 1)[-1],
    )
    ids = tuple(line.line_id for line in _compute(_recorded_plans_request()).lines)
    assert ids != RECORDED_LINE_IDS


# ------------------------------------------------------------------------------- replay determinism (T4-C)


def test_select_the_same_request_gives_byte_equal_output_at_two_wall_clock_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    request = _nine_line_request()
    first = _compute(request).model_dump_json()
    monkeypatch.setattr(time, "time", lambda: 4_000_000_000.0)
    second = _compute(request).model_dump_json()
    third = _compute(_nine_line_request()).model_dump_json()
    assert first == second == third


def test_select_a_mutant_that_reads_the_wall_clock_fails_the_byte_equal_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    real_stamp = handler_factory_floor._stamp
    monkeypatch.setattr(
        handler_factory_floor,
        "_stamp",
        lambda value: f"{real_stamp(value)}@{time.time()}",
    )
    request = _nine_line_request()
    monkeypatch.setattr(time, "time", lambda: 1.0)
    first = _compute(request).model_dump_json()
    monkeypatch.setattr(time, "time", lambda: 2.0)
    assert _compute(request).model_dump_json() != first


def test_select_output_follows_the_requests_time_and_not_the_clock() -> None:
    moved = _nine_line_request(generated_at=NOW + timedelta(days=20))
    # 20 days later the 60-minute-old ruling is out of the window: the time is the request's own.
    ids = {line.line_id for line in _compute(moved).lines}
    assert "ruling:2026-11-08T13:00:00Z" not in ids


# ------------------------------------------------------------------------------- coverage (T4-D)


def _cover(result: ModelFactoryFloorResult, name: str):  # type: ignore[no-untyped-def]
    return next(c for c in result.coverage if c.line_class == name)


def test_line_class_coverage_a_read_source_counts_its_lines() -> None:
    result = _compute(_nine_line_request())
    plan = _cover(result, "plan")
    assert (plan.state, plan.lines, plan.note) == ("read", 2, "")
    assert _cover(result, "declared").lines == 2
    assert _cover(result, "process").state == "read"


def test_line_class_coverage_a_stale_plans_today_keeps_its_lines_and_says_stale_with_its_time() -> (
    None
):
    stale_min = 3 * 24 * 60
    result = _compute(_nine_line_request(source_health=_health("stale", stale_min)))
    plan = _cover(result, "plan")
    assert plan.state == "stale"
    assert plan.read_at == (NOW - timedelta(minutes=stale_min)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert plan.lines == 2
    assert plan.note == f"UNREAD: plan selection from {plan.read_at}"
    assert "2026-11-02-charlie-plan" in {line.line_id for line in result.lines}


def test_line_class_coverage_a_plans_today_never_read_says_unread_with_no_class_two_count() -> (
    None
):
    result = _compute(_nine_line_request(source_health=_health("unread")))
    plan = _cover(result, "plan")
    assert plan.state == "unread"
    assert plan.lines is None
    assert plan.note == "UNREAD: plan selection, lines unknown"
    # Class 2 is empty: the paths are not taken from a source that was never read.
    assert [line.line_class for line in result.lines].count("plan") == 0
    # The overlay-declared plan the paths also named stays a declared line.
    assert "2026-10-31-bravo-plan" in {line.line_id for line in result.lines}


def test_line_class_coverage_a_source_the_caller_did_not_report_on_is_unread_never_read() -> (
    None
):
    result = _compute(_nine_line_request(source_health=()))
    assert {c.state for c in result.coverage if c.line_class != "process"} == {"unread"}
    assert all(c.lines is None for c in result.coverage if c.line_class != "process")


def test_line_class_coverage_is_the_worst_health_among_a_classs_sources() -> None:
    health = tuple(
        h.model_copy(update={"state": "read-empty"})
        if h.source == "running_evidence"
        else h
        for h in _health()
    )
    assert _cover(
        _compute(_nine_line_request(source_health=health)), "ticket"
    ).state == ("read-empty")
