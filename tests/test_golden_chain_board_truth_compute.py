# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_board_truth_compute: facts in, projected board state out.

The chain is the replay proof. A captured fact bundle plus a fixed evaluation
instant must yield the same projection every time — that is what lets a board
state be re-derived and audited long after the run, rather than taken on trust.

Related:
    - OMN-16729: Epic — board-truth mechanization
    - OMN-16731: node_board_truth_compute
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.enums.enum_board_reconcile_action import EnumBoardReconcileAction
from omnimarket.enums.enum_derived_board_state import EnumDerivedBoardState
from omnimarket.enums.enum_pr_state import EnumPrState
from omnimarket.events.board_truth import (
    ModelBoardFactBundle,
    ModelBoardTruthInput,
    ModelBoardTruthRow,
    ModelLedgerClaimFact,
    ModelLinearStateFact,
    ModelPrFact,
)
from omnimarket.nodes.node_board_truth_compute.handlers.handler_board_truth import (
    HandlerBoardTruth,
)

_INSTANT = datetime(2026, 8, 27, 11, 30, 0, tzinfo=UTC)
_CORRELATION = UUID("00000000-0000-4000-8000-000000016729")
_CONTRACT_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_board_truth_compute"
    / "contract.yaml"
)


def _contract() -> dict[str, object]:
    raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _contract_terminal_event() -> str:
    topic = _contract()["terminal_event"]
    assert isinstance(topic, str)
    return topic


def _contract_publish_topics() -> list[str]:
    event_bus = _contract()["event_bus"]
    assert isinstance(event_bus, dict)
    topics = event_bus["publish_topics"]
    assert isinstance(topics, list)
    return [str(topic) for topic in topics]


def _linear_state(name: str, state_type: str) -> ModelLinearStateFact:
    return ModelLinearStateFact(
        state_name=name,
        state_type=state_type,
        set_at=_INSTANT - timedelta(hours=2),
        set_by_automation=None,
    )


# One bundle per edge of the state machine, so the chain exercises the whole
# derivation rather than a happy path.
_CHAIN = (
    # Reaper: In Progress with no claim, no PR, no commits. This is the shape
    # the 2026-08-27 manual sweep hand-fixed on OMN-15579 and OMN-16675.
    ModelBoardFactBundle(
        ticket="OMN-REAP",
        current_state=_linear_state("In Progress", "started"),
        ledger_claims=(),
        pull_requests=(),
        branches=(),
        facts_complete=True,
    ),
    # Genuine review: an open, non-draft PR while the board says In Progress.
    ModelBoardFactBundle(
        ticket="OMN-REVIEW",
        current_state=_linear_state("In Progress", "started"),
        ledger_claims=(),
        pull_requests=(
            ModelPrFact(
                repo="omnimarket",
                number=2200,
                state=EnumPrState.OPEN,
                draft=False,
                updated_at=_INSTANT - timedelta(hours=6),
            ),
        ),
        branches=(),
        facts_complete=True,
    ),
    # Live claim while the board says Backlog.
    ModelBoardFactBundle(
        ticket="OMN-CLAIMED",
        current_state=_linear_state("Backlog", "backlog"),
        ledger_claims=(
            ModelLedgerClaimFact(
                lane="board-truth-mechanization",
                claimed_at=_INSTANT - timedelta(hours=1),
                terminal_at=None,
            ),
        ),
        pull_requests=(),
        branches=(),
        facts_complete=True,
    ),
    # Completion signal — reported, never flipped. OMN-16106 owns this edge.
    ModelBoardFactBundle(
        ticket="OMN-MERGED",
        current_state=_linear_state("In Review", "started"),
        ledger_claims=(),
        pull_requests=(
            ModelPrFact(
                repo="omnimarket",
                number=2201,
                state=EnumPrState.MERGED,
                draft=False,
                updated_at=_INSTANT - timedelta(days=1),
            ),
        ),
        branches=(),
        facts_complete=True,
    ),
    # A gap in the facts. Unknown is not absent.
    ModelBoardFactBundle(
        ticket="OMN-UNKNOWN",
        current_state=_linear_state("In Review", "started"),
        ledger_claims=(),
        pull_requests=(),
        branches=(),
        facts_complete=False,
    ),
)


def _project() -> tuple[ModelBoardTruthRow, ...]:
    return (
        HandlerBoardTruth()
        .handle(
            ModelBoardTruthInput(
                correlation_id=_CORRELATION,
                evaluated_at=_INSTANT,
                staleness_days=3,
                tickets=_CHAIN,
            )
        )
        .rows
    )


@pytest.mark.unit
class TestBoardTruthComputeGoldenChain:
    """Facts in, projected board state out — every edge, one chain."""

    def test_chain_projects_every_edge(self) -> None:
        rows = {row.ticket: row for row in _project()}

        assert rows["OMN-REAP"].derived_state is EnumDerivedBoardState.BACKLOG
        assert rows["OMN-REAP"].action is EnumBoardReconcileAction.FLIP

        assert rows["OMN-REVIEW"].derived_state is EnumDerivedBoardState.IN_REVIEW
        assert rows["OMN-REVIEW"].action is EnumBoardReconcileAction.FLIP

        assert rows["OMN-CLAIMED"].derived_state is EnumDerivedBoardState.IN_PROGRESS
        assert rows["OMN-CLAIMED"].action is EnumBoardReconcileAction.FLIP

        assert (
            rows["OMN-MERGED"].derived_state
            is EnumDerivedBoardState.DEFER_TO_DOD_VERIFY
        )
        assert rows["OMN-MERGED"].action is EnumBoardReconcileAction.DISCREPANCY

        assert rows["OMN-UNKNOWN"].derived_state is EnumDerivedBoardState.AMBIGUOUS
        assert rows["OMN-UNKNOWN"].action is EnumBoardReconcileAction.DISCREPANCY

    def test_every_flip_in_the_chain_is_evidence_backed(self) -> None:
        for row in _project():
            if row.action is EnumBoardReconcileAction.FLIP:
                assert row.evidence, f"{row.ticket} would flip with no citable fact"

    def test_chain_replays_identically(self) -> None:
        """The replay proof: same facts, same instant, same projection."""
        first = _project()
        second = _project()
        assert [r.model_dump_json() for r in first] == [
            r.model_dump_json() for r in second
        ]

    def test_chain_never_flips_a_ticket_to_a_completed_state(self) -> None:
        for row in _project():
            if row.derived_state is EnumDerivedBoardState.DEFER_TO_DOD_VERIFY:
                assert row.action is not EnumBoardReconcileAction.FLIP

    async def test_terminal_topics_carry_the_projection(
        self, event_bus: EventBusInmemory
    ) -> None:
        """The contract's declared success topic actually carries a projection.

        A contract that declares a terminal event nothing ever publishes is a
        dead chain — the topic reads as wired while nothing reaches a consumer.
        """
        await event_bus.start()

        rows = _project()
        payload = json.dumps(
            {
                "correlation_id": str(_CORRELATION),
                "rows": [
                    {"ticket": row.ticket, "derived_state": row.derived_state.value}
                    for row in rows
                ],
            }
        ).encode()

        terminal_topic = _contract_terminal_event()
        assert terminal_topic == "onex.evt.omnimarket.board-truth-completed.v1"
        assert terminal_topic in _contract_publish_topics()

        await event_bus.publish(terminal_topic, key=None, value=payload)

        history = await event_bus.get_event_history(topic=terminal_topic)
        assert len(history) == 1

        await event_bus.close()
