# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A declared line the build cannot draw is drawn stopped with its cause, and the ledger's open stops stop a line (OMN-21733).

The facts are the ones the board missed: three lines declared, one drawn; the wave plan sat at its first gate from the
blocked TERMINAL of 08:19:23Z until 22:35Z with its ask to the orchestrator unanswered. The overlay is in memory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorRequest,
    ModelFloorLedgerRow,
    ModelFloorManifest,
    ModelFloorManifestTask,
    ModelFloorPlanInput,
    ModelFloorPr,
    ModelFloorSelection,
    ModelFloorTableTask,
    ModelFloorWatcher,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFactoryFloorResult,
    ModelFloorLine,
)
from tests.nodes.node_factory_floor_compute.support import config

pytestmark = pytest.mark.unit

AT = datetime(2026, 10, 31, 22, 35, 0, tzinfo=UTC)
# The in-memory overlay: the three lines the board declared.
VALIDATORS = "validators-to-core-and-hardcoded-rules"
WAVE = "orchestration-alignment-wave-plan"
RUNTIME = "one-runtime"
DECLARED = (VALIDATORS, WAVE, RUNTIME)
EPIC = "OMN-21634"
ASK = "2026-10-31T08:17:58Z-retro-track-b"
HOLD_TEXT = (
    "ANDON HOLD (operator ruling 2026-10-29T01:30:28Z): no new dispatch, push or handoff of factory-plan "
    "(epic OMN-21433) work that is not validator logic until RELEASE or 2026-11-05T00:00Z. "
    "Validators-to-core plan work and any lane fixing a validator is exempt and continues."
)


def ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, 31, hour, minute, second, tzinfo=UTC)


def validators_plan(*, pr: str = "repo#7") -> ModelFloorPlanInput:
    return ModelFloorPlanInput(
        plan_path=f"beta/plans/2026-10-28-{VALIDATORS}.md",
        tickets=("OMN-21433",),
        table_tasks=(
            ModelFloorTableTask(
                label="T1",
                text="stage 0",
                tickets=("OMN-21433",),
                prs=(pr,),
                state="open-red",
            ),
        ),
    )


def wave_plan() -> ModelFloorPlanInput:
    return ModelFloorPlanInput(
        plan_path=f"beta/plans/2026-10-30-{WAVE}.md",
        tickets=(EPIC,),
        manifest=ModelFloorManifest(
            epic=EPIC,
            tasks=(
                ModelFloorManifestTask(id="W0.2", wave="0", tickets=("OMN-21636",)),
                ModelFloorManifestTask(
                    id="G0", wave="0", kind="gate", depends_on=("W0.2",)
                ),
            ),
        ),
    )


def ledger_row(
    when: datetime, kind: str, lane: str, **cells: Any
) -> ModelFloorLedgerRow:
    return ModelFloorLedgerRow(ts=when, kind=kind, lane=lane, **cells)


def blocked_terminal() -> ModelFloorLedgerRow:
    return ledger_row(
        ts(8, 19, 23),
        "TERMINAL",
        "retro-track-b",
        tickets=(EPIC,),
        outcome="blocked",
        text="Stopped at gate G0 as instructed, verdict fail.",
    )


def orchestrator_ask(
    row_id: str = ASK, when: datetime | None = None
) -> ModelFloorLedgerRow:
    return ledger_row(
        when or ts(8, 17, 58),
        "MSG",
        "retro-track-b",
        tickets=(EPIC,),
        row_id=row_id,
        to="orchestrator-31d9",
        text="Track B stopped at Wave 0, G0 not passed. Two decisions are needed.",
    )


def compute(
    plans: tuple[ModelFloorPlanInput, ...],
    *,
    declared: tuple[str, ...] = DECLARED,
    ledger: tuple[ModelFloorLedgerRow, ...] = (),
    prs: tuple[ModelFloorPr, ...] = (),
) -> ModelFactoryFloorResult:
    return compute_factory_floor(
        ModelFactoryFloorRequest(
            generated_at=AT,
            config=config(),
            selection=ModelFloorSelection(plan_path_contains=declared),
            plans=plans,
            watcher=ModelFloorWatcher(last_tick=ts(22, 30), prs=prs),
            ledger=ledger,
        )
    )


def by_id(result: ModelFactoryFloorResult) -> dict[str, ModelFloorLine]:
    return {line.line_id: line for line in result.lines}


# --------------------------------------------------------------------- a declared line with no plan


def test_a_declared_fragment_matching_no_plan_file_is_drawn_stopped_with_its_cause() -> (
    None
):
    result = compute((validators_plan(),))
    assert [line.declared_fragment for line in result.lines] == list(DECLARED)
    for token in (WAVE, RUNTIME):
        line = next(x for x in result.lines if x.declared_fragment == token)
        assert line.status_pill == "stopped"
        assert line.source == "undrawn"
        assert line.andon.flag is True
        assert line.andon.causes == (
            f"plan file not found: no plan path contains {token}",
        )
        assert line.andon.reason.startswith("plan file not found")
        assert line.stations == ()


def test_every_declared_line_that_has_a_plan_is_drawn_from_it_and_not_as_undrawn() -> (
    None
):
    result = compute((validators_plan(), wave_plan()))
    drawn = [line for line in result.lines if line.source != "undrawn"]
    assert [line.declared_fragment for line in drawn] == [VALIDATORS, WAVE]
    assert [
        line.declared_fragment for line in result.lines if line.source == "undrawn"
    ] == [RUNTIME]


def test_a_run_that_declares_no_line_draws_the_plans_it_is_given_and_no_undrawn_line() -> (
    None
):
    result = compute((validators_plan(),), declared=())
    assert [line.source for line in result.lines] == ["plan-task-table"]
    assert result.lines[0].declared_fragment == ""


def test_a_plan_that_matched_but_has_no_tasks_is_drawn_stopped_as_no_task_table() -> (
    None
):
    empty = ModelFloorPlanInput(plan_path=f"beta/plans/2026-10-30-{RUNTIME}.md")
    line = next(x for x in compute((empty,)).lines if x.declared_fragment == RUNTIME)
    assert line.source == "plan-task-table"
    assert line.status_pill == "stopped"
    assert line.andon.causes[0].startswith("no task table")


# --------------------------------------------------------------------- the ledger's open stops


def test_a_blocked_terminal_and_an_unanswered_ask_stop_the_line_with_cause_and_age() -> (
    None
):
    result = compute((wave_plan(),), ledger=(orchestrator_ask(), blocked_terminal()))
    line = by_id(result)[f"2026-10-30-{WAVE}"]
    assert line.status_pill == "stopped"
    causes = line.andon.causes
    assert (
        f"MSG {ASK} to orchestrator-31d9 unanswered for 857 min: Track B stopped"
        in causes[0]
    )
    assert (
        "lane retro-track-b ended blocked 855 min ago: Stopped at gate G0" in causes[1]
    )


def test_two_unanswered_asks_to_the_orchestrator_are_two_causes() -> None:
    second = orchestrator_ask("2026-10-31T08:18:20Z-retro-track-b", ts(8, 18, 20))
    line = by_id(compute((wave_plan(),), ledger=(orchestrator_ask(), second)))[
        f"2026-10-30-{WAVE}"
    ]
    asks = [c for c in line.andon.causes if c.startswith("MSG ")]
    assert len(asks) == 2
    assert line.andon.reason.endswith("(+1 more)")


def test_an_ack_that_names_the_ask_closes_it() -> None:
    answer = ledger_row(
        ts(9, 0), "ACK", "orchestrator-31d9", row_id="ack-1", re=ASK, tickets=(EPIC,)
    )
    line = by_id(compute((wave_plan(),), ledger=(orchestrator_ask(), answer)))[
        f"2026-10-30-{WAVE}"
    ]
    assert line.andon.causes == ()
    assert line.status_pill != "stopped"


def test_a_claim_by_another_lane_on_the_ticket_after_the_stop_resumes_the_line() -> (
    None
):
    resumed = ledger_row(
        ts(22, 22, 56), "CLAIM", "retro-w0-resume-2235", tickets=(EPIC,)
    )
    line = by_id(
        compute(
            (wave_plan(),), ledger=(orchestrator_ask(), blocked_terminal(), resumed)
        )
    )[f"2026-10-30-{WAVE}"]
    assert line.andon.causes == ()


def test_a_blocked_terminal_on_a_ticket_outside_the_plan_stops_nothing() -> None:
    elsewhere = blocked_terminal().model_copy(update={"tickets": ("OMN-1",)})
    line = by_id(compute((wave_plan(),), ledger=(elsewhere,)))[f"2026-10-30-{WAVE}"]
    assert line.andon.causes == ()


def test_a_message_to_another_lane_is_not_an_ask_to_the_orchestrator() -> None:
    chatter = orchestrator_ask().model_copy(update={"to": "retro-wave-plan-0225"})
    line = by_id(compute((wave_plan(),), ledger=(chatter,)))[f"2026-10-30-{WAVE}"]
    assert line.andon.causes == ()


# --------------------------------------------------------------------- the validators line


def validators_facts(*, red: bool) -> ModelFactoryFloorResult:
    hold = ledger_row(
        ts(21, 0),
        "HOLD",
        "andon-hold-0135",
        row_id="2026-10-29T01:31:43Z-andon-hold-0135",
        tickets=("OMN-21433",),
        until=datetime(2026, 11, 5, tzinfo=UTC),
        text=HOLD_TEXT,
    )
    gating = ModelFloorPr(
        key="repo#7",
        state="OPEN",
        red=red,
        ci="red" if red else "green",
        ci_note="repo-evidence / dod-verify" if red else "",
    )
    return compute((validators_plan(), wave_plan()), ledger=(hold,), prs=(gating,))


def test_the_validators_line_is_stopped_by_its_red_gating_pr_and_never_by_the_hold_that_exempts_it() -> (
    None
):
    line = by_id(validators_facts(red=True))[f"2026-10-28-{VALIDATORS}"]
    assert line.status_pill == "stopped"
    assert line.andon.causes == ("PR repo#7 CI red (repo-evidence / dod-verify)",)


def test_the_validators_line_is_not_stopped_when_only_the_exempting_hold_is_open() -> (
    None
):
    line = by_id(validators_facts(red=False))[f"2026-10-28-{VALIDATORS}"]
    assert line.status_pill != "stopped"
    assert line.andon.causes == ()


# --------------------------------------------------------------------- no hard-coded line names


def test_the_changed_source_names_none_of_the_declared_lines() -> None:
    root = Path(__file__).resolve().parents[3] / "src" / "omnimarket"
    scanned = [
        path
        for pattern in (
            "nodes/node_factory_floor_*/**/*.py",
            "nodes/node_factory_floor_*/**/*.yaml",
            "models/model_factory_floor_input.py",
        )
        for path in root.glob(pattern)
    ]
    assert any(
        path.name == "handler_factory_floor.py" for path in scanned
    )  # the scan reaches the handler
    for path in scanned:
        text = path.read_text(encoding="utf-8")
        for token in DECLARED:
            assert token not in text, f"{path.name} names {token}"
