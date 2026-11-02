# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A task read from a plan's own paragraphs links only through its tickets and pull requests, never its bare id."""

from __future__ import annotations

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorLedgerRow,
    ModelFloorManifest,
    ModelFloorManifestTask,
    ModelFloorPlanInput,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    _index,
    _resumed,
    _task_ids,
    compute_factory_floor,
)
from tests.nodes.node_factory_floor_compute.support import merge, pr, request, row

pytestmark = pytest.mark.unit

PLAN = "beta/plans/2026-11-01-some-plan.md"


def plan_of(*, scoped: bool, tickets: tuple[str, ...] = ()) -> ModelFloorPlanInput:
    return ModelFloorPlanInput(
        plan_path=PLAN,
        title="Some plan",
        manifest=ModelFloorManifest(
            tasks=(
                ModelFloorManifestTask(
                    id="T5", title="T5 writers", tickets=tickets, scoped=scoped
                ),
            )
        ),
    )


def state_of(*, scoped: bool, tickets: tuple[str, ...] = ()) -> tuple[str, str]:
    # Another plan's merged pull request and finished lane both say "T5" in their own words.
    result = compute_factory_floor(
        request(
            plans=(plan_of(scoped=scoped, tickets=tickets),),
            prs=(pr("repo#9", "MERGED", title="T5: a different plan's task"),),
            merges=(merge("repo#9", 30, title="T5: a different plan's task"),),
            ledger=(
                row("CLAIM", 90, tickets=("OMN-5",)),
                row(
                    "TERMINAL", 60, outcome="done", reason="finished T5 of another plan"
                ),
            ),
        )
    )
    item = result.lines[0].stations[0].items[0]
    return item.state, item.note


def test_a_bare_id_in_another_plans_words_does_not_finish_a_scoped_task() -> None:
    state, note = state_of(scoped=True)
    assert state == "prep", note


def test_the_same_words_do_finish_an_unscoped_task() -> None:
    # The control: without the flag the manifest rules link by id, so the positive control for the case above holds.
    state, note = state_of(scoped=False)
    assert state == "done", note


def test_a_scoped_task_is_finished_by_the_merge_of_its_ticket() -> None:
    result = compute_factory_floor(
        request(
            plans=(plan_of(scoped=True, tickets=("OMN-7",)),),
            prs=(
                pr(
                    "repo#4",
                    "MERGED",
                    title="feat(OMN-7): the work",
                    tickets=("OMN-7",),
                ),
            ),
            merges=(
                merge("repo#4", 30, title="feat(OMN-7): the work", tickets=("OMN-7",)),
            ),
        )
    )
    line = result.lines[0]
    assert line.stations[0].items[0].state == "done"
    assert line.percent_built == 100


def test_resumed_agrees_with_the_definition_on_every_pair() -> None:
    # The definition: another lane claimed one of the row's tickets after it. The index must answer the same.
    ledger = (
        row("CLAIM", 100, "lane-a", tickets=("OMN-1",)),
        row("CLAIM", 90, "lane-b", tickets=("OMN-1", "OMN-2")),
        row("CLAIM", 80, "lane-a", tickets=("OMN-2",)),
        row("TERMINAL", 70, "lane-b", tickets=("OMN-1",), outcome="blocked"),
        row("TERMINAL", 60, "lane-c", tickets=("OMN-3",), outcome="blocked"),
        row("CLAIM", 50, "lane-a", tickets=("OMN-3",)),
        row("TERMINAL", 40, "lane-a", tickets=("OMN-3",), outcome="blocked"),
    )
    asked = request(ledger=ledger)
    index = _index(asked, _task_ids(asked.plans))

    def by_definition(target: ModelFloorLedgerRow) -> bool:
        return any(
            other.kind == "CLAIM"
            and other.ts > target.ts
            and other.lane != target.lane
            and bool(set(other.tickets) & set(target.tickets))
            for other in ledger
        )

    for target in ledger:
        assert _resumed(target, index) == by_definition(target), target
