# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An item is done only when merged, running on dev and integration-proven; anything less is INTEGRATING."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.model_factory_floor_input import (
    ModelFloorLedgerRow,
    ModelFloorLinearIssue,
    ModelFloorManifest,
    ModelFloorPlanInput,
    ModelFloorTableTask,
)
from omnimarket.models.model_merged_running_count import ModelMrcEvidence
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    compute_factory_floor,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFloorItem,
    ModelFloorLine,
)
from tests.nodes.node_factory_floor_compute.support import (
    ago,
    merge,
    pr,
    request,
    row,
    task,
)

pytestmark = pytest.mark.unit

PLAN = "beta/plans/2026-11-02-integration-plan.md"
TICKET = "OMN-500"
PR = "OmniNode-ai/omnimarket#9001"


def plan() -> ModelFloorPlanInput:
    return ModelFloorPlanInput(
        plan_path=PLAN,
        title="Integration plan",
        manifest=ModelFloorManifest(
            tasks=(
                task("A1", "0", tickets=(TICKET,), prs=(PR,)),
                task("A2", "0"),
            )
        ),
    )


def running(number: int = 9001, repo: str = "omnimarket") -> ModelMrcEvidence:
    return ModelMrcEvidence(
        repo=repo,
        kind="deploy-receipt",
        source="deploy agent receipt",
        number=number,
        observed_at=ago(5),
    )


def proof(
    verdict: str = "pass", minutes_ago: float = 4, **cells: Any
) -> ModelFloorLedgerRow:
    named: dict[str, Any] = {"prs": (PR,), **cells}
    return row("STATUS", minutes_ago, state="integration", verdict=verdict, **named)


def a1(**facts: Any) -> tuple[ModelFloorItem, ModelFloorLine]:
    base: dict[str, Any] = {
        "plans": (plan(),),
        "prs": (pr(PR, "MERGED"),),
        "merges": (merge(PR, 30),),
    }
    result = compute_factory_floor(request(**{**base, **facts}))
    line = result.lines[0]
    item = next(i for s in line.stations for i in s.items if i.id == "A1")
    return item, line


def test_integrating_merged_but_not_running_is_not_done_and_not_built() -> None:
    item, line = a1(running_evidence=(), ledger=(proof(),))
    assert item.state == "integrating"
    assert "not running on dev" in item.note
    assert "omnimarket#9001" in item.note
    assert line.percent_built == 0


def test_integrating_running_evidence_for_another_merge_does_not_count() -> None:
    item, _ = a1(running_evidence=(running(number=7777),), ledger=(proof(),))
    assert item.state == "integrating"
    assert "not running on dev" in item.note


def test_integrating_merged_and_running_without_a_proof_says_so() -> None:
    item, line = a1(running_evidence=(running(),))
    assert item.state == "integrating"
    assert "integration proof" in item.note
    assert line.percent_built == 0


def test_integrating_a_failed_proof_is_unproven() -> None:
    item, _ = a1(running_evidence=(running(),), ledger=(proof("fail"),))
    assert item.state == "integrating"
    assert "failed" in item.note


def test_integrating_a_proof_older_than_the_merge_is_unproven() -> None:
    item, _ = a1(running_evidence=(running(),), ledger=(proof(minutes_ago=60),))
    assert item.state == "integrating"
    assert "integration proof" in item.note


def test_integrating_unread_running_evidence_is_never_taken_as_running() -> None:
    item, _ = a1(ledger=(proof(),))
    assert item.state == "integrating"
    assert "unread" in item.note


def test_integrating_counts_as_building_not_done_and_does_not_open_dependents() -> None:
    dependent = ModelFloorPlanInput(
        plan_path=PLAN,
        manifest=ModelFloorManifest(
            tasks=(
                task("A1", "0", tickets=(TICKET,), prs=(PR,)),
                task("A2", "1", depends_on=("A1",)),
            )
        ),
    )
    item, line = a1(plans=(dependent,), running_evidence=())
    assert item.state == "integrating"
    assert line.status_pill == "building"
    waiting = next(i for s in line.stations for i in s.items if i.id == "A2")
    assert waiting.state == "wait"


def test_merged_and_running_with_a_passed_proof_is_done_and_built() -> None:
    item, line = a1(running_evidence=(running(),), ledger=(proof(),))
    assert item.state == "done"
    assert "running on dev" in item.note
    assert line.percent_built == 50


def test_merged_and_running_proof_may_name_the_ticket_instead_of_the_pr() -> None:
    item, _ = a1(
        running_evidence=(running(),),
        ledger=(proof(prs=(), tickets=(TICKET,)),),
    )
    assert item.state == "done"


def test_merged_and_running_evidence_names_the_repository_by_its_bare_name() -> None:
    item, _ = a1(
        running_evidence=(running(repo="OmniNode-ai/omnimarket"),),
        ledger=(proof(),),
    )
    assert item.state == "done"


def test_merged_and_running_the_newest_proof_decides() -> None:
    item, _ = a1(
        running_evidence=(running(),),
        ledger=(proof("pass", 10), proof("fail", 2)),
    )
    assert item.state == "integrating"


def test_integrating_linear_done_alone_never_yields_done() -> None:
    result = compute_factory_floor(
        request(
            plans=(plan(),),
            linear=(
                ModelFloorLinearIssue(
                    id=TICKET, status="Done", status_type="completed"
                ),
            ),
            running_evidence=(),
        )
    )
    item = result.lines[0].stations[0].items[0]
    assert item.state == "integrating"
    assert item.note.startswith("Linear done")
    assert result.lines[0].percent_built == 0


def test_integrating_a_done_terminal_alone_never_yields_done() -> None:
    result = compute_factory_floor(
        request(
            plans=(plan(),),
            ledger=(row("TERMINAL", 20, tickets=(TICKET,), outcome="done"),),
            running_evidence=(),
        )
    )
    item = result.lines[0].stations[0].items[0]
    assert item.state == "integrating"
    assert item.note.startswith("terminal done")
    assert result.lines[0].percent_built == 0


def test_integrating_a_handed_off_terminal_whose_prs_merged_is_not_yet_done() -> None:
    other = "OmniNode-ai/omnimarket#9002"
    result = compute_factory_floor(
        request(
            plans=(plan(),),
            prs=(pr(other, "MERGED"),),
            ledger=(
                row(
                    "TERMINAL",
                    20,
                    tickets=(TICKET,),
                    outcome="handed-off",
                    prs=(other,),
                ),
            ),
            running_evidence=(),
        )
    )
    item = result.lines[0].stations[0].items[0]
    assert item.state == "integrating"
    assert "omnimarket#9002" in item.note


def test_merged_and_running_a_done_terminal_with_evidence_and_proof_is_done() -> None:
    result = compute_factory_floor(
        request(
            plans=(plan(),),
            prs=(pr(PR, "MERGED"),),
            ledger=(
                row("TERMINAL", 20, tickets=(TICKET,), outcome="done"),
                proof(),
            ),
            running_evidence=(running(),),
        )
    )
    assert result.lines[0].stations[0].items[0].state == "done"


def test_integrating_the_plans_today_table_follows_the_same_rule() -> None:
    table = ModelFloorPlanInput(
        plan_path="beta/plans/2026-11-02-table-plan.md",
        table_tasks=(
            ModelFloorTableTask(
                label="T1", tickets=(TICKET,), prs=(PR,), state="merged"
            ),
        ),
    )
    unproven = compute_factory_floor(
        request(
            plans=(table,),
            merges=(merge(PR, 30, title=f"feat({TICKET}): first"),),
            running_evidence=(),
        )
    )
    assert unproven.lines[0].stations[0].items[0].state == "integrating"
    assert unproven.lines[0].percent_built == 0
    proven = compute_factory_floor(
        request(
            plans=(table,),
            merges=(merge(PR, 30, title=f"feat({TICKET}): first"),),
            running_evidence=(running(),),
            ledger=(proof(),),
        )
    )
    assert proven.lines[0].stations[0].items[0].state == "done"
    assert proven.lines[0].percent_built == 100


def test_integrating_sources_record_whether_running_evidence_was_read() -> None:
    unread = compute_factory_floor(request(plans=(plan(),)))
    assert unread.sources.running_evidence_read is False
    read = compute_factory_floor(request(plans=(plan(),), running_evidence=()))
    assert read.sources.running_evidence_read is True


def test_integrating_vocabulary_is_declared_by_the_contract_not_the_handler() -> None:
    from tests.nodes.node_factory_floor_compute.support import config

    cfg = config()
    assert cfg.integration_state == "integration"
    assert cfg.integration_pass_verdicts == ("pass",)
    assert cfg.integration_fail_verdicts == ("fail",)
