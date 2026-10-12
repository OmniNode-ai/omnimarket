# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A second lane starting in a held worktree is refused naming holder and release path."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_worktree_lease_compute.handlers.handler_worktree_lease import (
    HandlerWorktreeLeaseCompute,
)
from omnimarket.nodes.node_worktree_lease_compute.models.model_worktree_lease import (
    EnumWorktreeLeaseReason as Reason,
)
from omnimarket.nodes.node_worktree_lease_compute.models.model_worktree_lease import (
    EnumWorktreeLeaseVerdict as Verdict,
)
from omnimarket.nodes.node_worktree_lease_compute.models.model_worktree_lease import (
    ModelWorktreeLeaseRequest,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
ROOT = "/trees"
TREE = "/trees/OMN-100/repo"
CLAIM = "2026-10-07T10:00:00Z | CLAIM | lane=holder-1 | ticket=OMN-100 | scope"


def ask(ledger: str, lane: str = "second-2", path: str = TREE, **kw: object):
    return HandlerWorktreeLeaseCompute().handle(
        ModelWorktreeLeaseRequest(
            requester_lane=lane,
            worktree_path=path,
            root=ROOT,
            ledger_text=ledger,
            now=NOW,
            **kw,
        )
    )


def test_second_lane_in_a_held_worktree_is_refused_naming_holder_and_release_path() -> (
    None
):
    decision = ask(CLAIM)
    assert decision.verdict == Verdict.REFUSED
    assert decision.reason == Reason.HELD_BY_OTHER_LANE
    assert decision.holder_lane == "holder-1"
    assert decision.holder_claim_row == CLAIM
    assert decision.refusal is not None
    assert "lane holder-1" in decision.refusal
    release = decision.release_path
    assert release is not None
    assert (
        "RELEASE | lane=holder-1 | re=2026-10-07T10:00:00Z | ticket=OMN-100" in release
    )
    assert "CLAIM | lane=<your lane> | ticket=OMN-100" in release
    assert release in decision.refusal


def test_the_holder_itself_is_granted() -> None:
    decision = ask(CLAIM, lane="holder-1")
    assert decision.verdict == Verdict.GRANTED
    assert decision.reason == Reason.HELD_BY_REQUESTER
    assert decision.release_path is None


def test_an_unclaimed_worktree_is_granted() -> None:
    other = "2026-10-07T10:00:00Z | CLAIM | lane=holder-1 | ticket=OMN-200 | scope"
    decision = ask(other)
    assert decision.verdict == Verdict.GRANTED
    assert decision.reason == Reason.UNCLAIMED


@pytest.mark.parametrize(
    "ending",
    [
        "2026-10-07T10:30:00Z | RELEASE | lane=holder-1 | re=2026-10-07T10:00:00Z",
        "2026-10-07T10:30:00Z | TERMINAL | lane=holder-1 | outcome=done",
    ],
)
def test_a_released_or_ended_claim_no_longer_holds(ending: str) -> None:
    decision = ask(f"{CLAIM}\n{ending}")
    assert decision.verdict == Verdict.GRANTED
    assert decision.reason == Reason.UNCLAIMED


def test_a_claim_silent_past_the_staleness_bound_no_longer_holds() -> None:
    stale = "2026-10-06T21:00:00Z | CLAIM | lane=holder-1 | ticket=OMN-100 | scope"
    fresh = "2026-10-07T01:00:00Z | CLAIM | lane=holder-1 | ticket=OMN-100 | scope"
    assert ask(stale).verdict == Verdict.GRANTED  # 15h silent, bound 12h
    assert ask(fresh).verdict == Verdict.REFUSED  # 11h silent, same shape
    assert ask(fresh, stale_after_hours=10.0).verdict == Verdict.GRANTED


def test_a_ticket_id_that_is_a_prefix_of_the_claimed_one_is_not_held() -> None:
    assert ask(CLAIM, path="/trees/OMN-10/repo").verdict == Verdict.GRANTED
    assert ask(CLAIM, path="/trees/OMN-100-other/repo").verdict == Verdict.GRANTED


def test_a_claim_naming_the_exact_path_holds_it() -> None:
    row = f"2026-10-07T10:00:00Z | CLAIM | lane=holder-1 | worktree={TREE}"
    decision = ask(row, path=TREE)
    assert decision.verdict == Verdict.REFUSED
    assert decision.release_path is not None
    assert "ticket=OMN-100" in decision.release_path


def test_a_second_other_lane_is_listed_beside_the_first() -> None:
    second = "2026-10-07T10:10:00Z | CLAIM | lane=holder-2 | ticket=OMN-100 | scope"
    decision = ask(f"{CLAIM}\n{second}")
    assert decision.holder_lane == "holder-1"
    assert decision.other_holder_lanes == ("holder-2",)


@pytest.mark.parametrize("ledger", ["", "no dated rows here\n| legacy | table |"])
def test_a_ledger_with_no_dated_row_refuses_rather_than_granting(ledger: str) -> None:
    decision = ask(ledger)
    assert decision.verdict == Verdict.REFUSED
    assert decision.reason == Reason.LEDGER_UNREADABLE
    assert decision.holder_lane is None


def test_contract_topics_match_the_node_name() -> None:
    node = (
        Path(__file__).resolve().parents[4]
        / "src/omnimarket/nodes/node_worktree_lease_compute"
    )
    contract = yaml.safe_load((node / "contract.yaml").read_text())
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.worktree-lease-evaluate.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.worktree-lease-evaluated.v1"
    ]


def test_contract_declares_the_decision_output() -> None:
    node = (
        Path(__file__).resolve().parents[4]
        / "src/omnimarket/nodes/node_worktree_lease_compute"
    )
    contract = yaml.safe_load((node / "contract.yaml").read_text())
    assert set(contract["outputs"]) == {"decision"}
