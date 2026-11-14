# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam test: the query node's open-claims line shape and the plan owner's parser (OMN-24445).

Two pieces share one line shape: ``node_work_ledger_query_effect`` renders each open claim
and ``node_work_ledger_reader_effect`` carries the lines; ``node_plan_owner_check_effect``
parses them. This runs the real query node handler over a state page through the real reader
handler into the plan owner handler, on the in-memory path. It fails when either side changes
the shape: a renamed ``newest-row=`` cell turns OWNED into UNKNOWN (never MISSING). The same
shared model is the reader's ``ModelReaderResult``.
"""

from __future__ import annotations

import pytest

from omnimarket.models.model_work_ledger_reader import (
    EnumReaderQuery,
    EnumReaderStatus,
    ModelReaderRequest,
    ModelReaderResult,
)
from omnimarket.models.plan_owner_check import (
    EnumPlanOwnerVerdict,
    ModelPlanOwnerOverlay,
    ModelPlanOwnerRequest,
    ModelPlanOwnerRule,
    ModelPlanOwnerVerdict,
)
from omnimarket.nodes.node_plan_owner_check_effect.handlers.handler_plan_owner_check import (
    HandlerPlanOwnerCheck,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    WorkLedgerReadError,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import InMemoryEventSink
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from tests.unit.nodes.node_plan_owner_check_effect.test_plan_owner_chain_events import (
    ListSink,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    NOW,
    FakeStateReader,
    claim_entity,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_chain_events import (
    QueryNodePointRead,
    overlay,
)

pytestmark = pytest.mark.unit


def owner_overlay(**changes: object) -> ModelPlanOwnerOverlay:
    """The plan owner overlay of this seam: the fixtures' lane rule, with overrides."""
    rule = ModelPlanOwnerRule(
        finding="cutover-plan-owner",
        lane_prefixes=("lane-",),
        tickets=("OMN-19609",),
        live_window_minutes=60,
        why="no live lane CLAIM names the cutover plan",
        fix="start a ledger cutover lane",
        unknown_fix="read the open-claims answer and fix the read it names",
    )
    return ModelPlanOwnerOverlay(
        reader="cutover-owner",
        claims_window_hours=1,
        rules=(rule.model_copy(update=changes),),
        journal_env="J",
    )


def verdict_over(
    reader_state: FakeStateReader, **rule_changes: object
) -> tuple[ModelReaderResult, ModelPlanOwnerVerdict]:
    reader = HandlerWorkLedgerReader(
        overlay(moved_readers=("cutover-owner",)),
        QueryNodePointRead(reader_state),
        InMemoryEventSink(),
        clock=lambda: NOW,
    )
    answer = reader.handle(
        ModelReaderRequest(reader="cutover-owner", query=EnumReaderQuery.OPEN_CLAIMS)
    )
    verdict = HandlerPlanOwnerCheck(
        owner_overlay(**rule_changes), ListSink(), clock=lambda: NOW
    ).handle(ModelPlanOwnerRequest(finding="cutover-plan-owner", node_answer=answer))
    return answer, verdict


def test_the_real_lane_claim_is_owned_through_the_real_reader() -> None:
    answer, verdict = verdict_over(FakeStateReader((claim_entity(),)))
    assert answer.status is EnumReaderStatus.OK
    assert answer.lines[0].startswith("open-claims since=beginning ")
    assert answer.lines[1].startswith("OPEN-CLAIM lane=lane-x ")
    assert verdict.verdict is EnumPlanOwnerVerdict.OWNED
    assert verdict.owners == ("lane-x",)


def test_a_claim_older_than_the_rule_window_is_missing() -> None:
    _, verdict = verdict_over(
        FakeStateReader((claim_entity(),)), live_window_minutes=20
    )
    assert verdict.verdict is EnumPlanOwnerVerdict.MISSING


def test_a_lane_the_rule_does_not_name_is_missing() -> None:
    _, verdict = verdict_over(
        FakeStateReader((claim_entity(),)), lane_prefixes=("other-",)
    )
    assert verdict.verdict is EnumPlanOwnerVerdict.MISSING


def test_an_empty_state_is_missing_only_because_the_node_answered() -> None:
    answer, verdict = verdict_over(FakeStateReader(()))
    assert answer.status is EnumReaderStatus.OK
    assert answer.lines == (
        "open-claims since=beginning claims=0 open=0 "
        "ledger-newest=2026-11-02T02:59:30Z now=2026-11-02T03:00:00Z",
    )
    assert verdict.verdict is EnumPlanOwnerVerdict.MISSING


def test_a_database_fault_is_unknown_never_missing() -> None:
    answer, verdict = verdict_over(
        FakeStateReader(error=WorkLedgerReadError("database unavailable"))
    )
    assert answer.status is EnumReaderStatus.UNREADABLE
    assert verdict.verdict is EnumPlanOwnerVerdict.UNKNOWN
    assert verdict.reason.startswith("node-read-failed:")


def test_break_a_renamed_newest_row_cell_is_unknown_never_missing() -> None:
    """The shape guard: if the rendered line drops ``newest-row=`` the seam is loud, not MISSING."""
    answer, _ = verdict_over(FakeStateReader((claim_entity(),)))
    broken = answer.model_copy(
        update={
            "lines": tuple(
                line.replace("newest-row=", "newest_row=") for line in answer.lines
            )
        }
    )
    verdict = HandlerPlanOwnerCheck(
        owner_overlay(), ListSink(), clock=lambda: NOW
    ).handle(ModelPlanOwnerRequest(finding="cutover-plan-owner", node_answer=broken))
    assert verdict.verdict is EnumPlanOwnerVerdict.UNKNOWN
    assert verdict.reason == "unparseable-claim-line"
