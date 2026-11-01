# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A HOLD stops a line only when it does not exempt it, shows its own text as the reason, and a red PR is a real stop (OMN-21733)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorLedgerRow,
    ModelFloorPlanInput,
    ModelFloorTableTask,
    TableTaskState,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFloorLine,
)
from tests.nodes.node_factory_floor_compute.support import NOW, pr, request, row

pytestmark = pytest.mark.unit

# The text of the real hold (ledger row 2026-10-29T01:31:43Z-andon-hold-0135), cut to its sentences.
HOLD_TEXT = (
    "ANDON HOLD (operator ruling 2026-10-29T01:30:28Z): no new dispatch, push or handoff of factory-plan "
    "(epic OMN-21433) work that is not validator logic until RELEASE or 2026-11-05T00:00Z. "
    "Validators-to-core plan work and any lane fixing a validator is exempt and continues. "
    "Operator: need to get the validation logic done first."
)


def plan(
    slug: str, *, prs: tuple[str, ...] = (), state: TableTaskState = "open-green"
) -> ModelFloorPlanInput:
    return ModelFloorPlanInput(
        plan_path=f"beta/plans/2026-10-28-{slug}.md",
        title=slug,
        tickets=("OMN-21433",),
        table_tasks=(
            ModelFloorTableTask(
                label="T1",
                text="first",
                tickets=("OMN-1",),
                prs=prs,
                state=state,
            ),
        ),
    )


def hold(reason: str = "") -> ModelFloorLedgerRow:
    return row(
        "HOLD",
        60,
        "andon-hold-0135",
        row_id="2026-10-29T01:31:43Z-andon-hold-0135",
        tickets=("OMN-21433",),
        until=NOW + timedelta(days=5),
        reason=reason,
        text=HOLD_TEXT,
    )


def lines(*plans: ModelFloorPlanInput, **extra: Any) -> dict[str, ModelFloorLine]:
    result = compute_factory_floor(request(plans=plans, ledger=(hold(),), **extra))
    return {line.line_id: line for line in result.lines}


def test_the_plan_a_hold_names_as_exempt_is_not_stopped_by_it_and_the_other_plan_is() -> (
    None
):
    drawn = lines(
        plan("validators-to-core-and-hardcoded-rules"),
        plan("orchestration-alignment-wave-plan"),
    )
    exempt = drawn["2026-10-28-validators-to-core-and-hardcoded-rules"]
    held = drawn["2026-10-28-orchestration-alignment-wave-plan"]
    assert (exempt.andon.flag, exempt.status_pill) == (False, "building")
    assert held.andon.flag is True


def test_the_reason_a_hold_shows_is_its_own_text_never_an_empty_string() -> None:
    held = lines(plan("orchestration-alignment-wave-plan"))[
        "2026-10-28-orchestration-alignment-wave-plan"
    ]
    reason = held.andon.reason
    assert reason.startswith("HOLD 2026-10-29T01:31:43Z-andon-hold-0135: ANDON HOLD")
    assert not reason.rstrip().endswith(":")


def test_a_line_whose_part_has_a_red_open_pull_request_is_stopped_by_that_cause() -> (
    None
):
    red = pr("repo#7", red=True).model_copy(
        update={"ci": "red", "ci_note": "3 red of 150"}
    )
    drawn = lines(
        plan(
            "validators-to-core-and-hardcoded-rules", prs=("repo#7",), state="open-red"
        ),
        prs=(red,),
    )
    line = drawn["2026-10-28-validators-to-core-and-hardcoded-rules"]
    assert line.andon.flag is True
    assert line.andon.causes == ("PR repo#7 CI red (3 red of 150)",)
    assert line.status_pill == "stopped"


def test_a_green_open_pull_request_stops_nothing() -> None:
    drawn = lines(
        plan("validators-to-core-and-hardcoded-rules", prs=("repo#7",)),
        prs=(pr("repo#7"),),
    )
    assert (
        drawn["2026-10-28-validators-to-core-and-hardcoded-rules"].andon.flag is False
    )
