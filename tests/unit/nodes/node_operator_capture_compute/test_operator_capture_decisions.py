# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_operator_capture_compute: rows, open asks and drift (OMN-20905, OMN-20906, OMN-20907)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.models.operator_capture import (
    EnumClassifierSource,
    EnumDriftRelation,
    EnumPromptOrigin,
    EnumUtteranceKind,
    ModelCaptureRowsRequest,
    ModelOpenAsksRequest,
    ModelRulingDriftRequest,
    ModelUtterance,
    ModelUtteranceClassification,
    ModelUtteranceItem,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_capture_rows import (
    HandlerCaptureRows,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_open_asks import (
    HandlerOpenAsks,
    has_closing_evidence,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_ruling_drift import (
    HandlerRulingDrift,
    prior_rulings_from_rows,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_utterance_classify import (
    item_id,
)

pytestmark = pytest.mark.unit

K = EnumUtteranceKind
NOW = datetime(2026, 10, 10, 18, 0, 0, tzinfo=UTC)
TEXT = 'There should be no cloud work in M4.\nCan you check the "h201" floor | today?'


def _utterance() -> ModelUtterance:
    return ModelUtterance(
        capture_id="cap0001",
        text=TEXT,
        session_id="sess-1",
        source="claude-code:remote",
        captured_at=NOW,
    )


def _item(kind: EnumUtteranceKind, quote: str, subject: str) -> ModelUtteranceItem:
    return ModelUtteranceItem(
        item_id=item_id("cap0001", quote),
        kind=kind,
        quote=quote,
        subject=subject,
        confidence=0.9,
    )


DECISION = _item(K.DECISION, "There should be no cloud work in M4.", "cloud work m4")
ASK = _item(K.ASK, 'Can you check the "h201" floor | today?', "h201 floor")


def _classification(
    *items: ModelUtteranceItem, drops: tuple[str, ...] = ()
) -> ModelUtteranceClassification:
    return ModelUtteranceClassification(
        capture_id="cap0001",
        origin=EnumPromptOrigin.OPERATOR,
        classifier=EnumClassifierSource.DELEGATED,
        classifier_model="Qwen3.8-27B",
        reason="delegated answer accepted",
        items=items,
        drops=drops,
    )


# -- rows -------------------------------------------------------------------------------------


def test_rows_one_per_item_with_verbatim_words_and_session() -> None:
    rows = (
        HandlerCaptureRows()
        .handle(
            ModelCaptureRowsRequest(
                utterance=_utterance(),
                classification=_classification(DECISION, ASK),
                stamp=NOW,
            )
        )
        .rows
    )
    assert len(rows) == 2
    decision, ask = rows
    assert decision.startswith(
        "2026-10-10T18:00:00Z | STATUS | lane=operator-capture | kind=decision"
    )
    assert "session=sess-1" in decision
    assert "source=claude-code:remote" in decision
    assert "classifier=delegated:Qwen3.8-27B" in decision
    assert decision.endswith('"There should be no cloud work in M4."')
    assert "verbatim=normalized" not in decision
    ask_ref = "ask-" + ASK.item_id.removeprefix("cap-")
    assert f"ask={ask_ref} | state=open" in ask
    assert "verbatim=normalized" in ask
    assert ask.endswith("\"Can you check the ''h201'' floor ¦ today?\"")
    assert ask.count(" | ") == decision.count(" | ") + 3


def test_rows_none_for_machine_and_drop_rows_for_drops() -> None:
    machine = ModelUtteranceClassification(
        capture_id="cap0001",
        origin=EnumPromptOrigin.MACHINE,
        classifier=EnumClassifierSource.HEURISTIC,
        reason="machine injection",
    )
    assert (
        HandlerCaptureRows()
        .handle(
            ModelCaptureRowsRequest(
                utterance=_utterance(), classification=machine, stamp=NOW
            )
        )
        .rows
        == ()
    )
    rows = (
        HandlerCaptureRows()
        .handle(
            ModelCaptureRowsRequest(
                utterance=_utterance(),
                classification=_classification(drops=("ask-0123456789",)),
                stamp=NOW,
            )
        )
        .rows
    )
    assert len(rows) == 1
    assert (
        "closes-ask=ask-0123456789 | state=dropped | evidence=operator-drop" in rows[0]
    )


def test_rows_carry_the_drift_flag() -> None:
    drift = HandlerRulingDrift().handle(
        ModelRulingDriftRequest(
            item=DECISION, prior=prior_rulings_from_rows([RULING_SAME])
        )
    )
    rows = (
        HandlerCaptureRows()
        .handle(
            ModelCaptureRowsRequest(
                utterance=_utterance(),
                classification=_classification(DECISION),
                drift={DECISION.item_id: drift},
                stamp=NOW,
            )
        )
        .rows
    )
    assert "drift=re-ruled | relation=reaffirms | prior=2026-09-22T20:35:26Z" in rows[0]


# -- open asks --------------------------------------------------------------------------------

ASK_ROW = (
    "2026-10-08T10:00:00Z | STATUS | lane=operator-capture | kind=ask | item=cap-0123456789 | "
    'ask=ask-0123456789 | state=open | session=s1 | source=claude-code:local | "Fix the dashboard projection."'
)
ASK_ROW_2 = (
    "2026-10-10T16:00:00Z | STATUS | lane=operator-capture | kind=ask | item=cap-abcdefabcd | "
    'ask=ask-abcdefabcd | state=open | session=s2 | source=claude-code:remote | "Check the h201 floor."'
)


def test_open_asks_stay_open_without_a_closing_row() -> None:
    folded = HandlerOpenAsks().handle(
        ModelOpenAsksRequest(rows=(ASK_ROW, ASK_ROW_2), now=NOW)
    )
    assert [a.ask_id for a in folded.open_asks] == ["ask-0123456789", "ask-abcdefabcd"]
    assert [a.overdue for a in folded.open_asks] == [True, False]
    assert folded.open_asks[0].words == "Fix the dashboard projection."


@pytest.mark.parametrize(
    "evidence",
    [
        "omnidash#412",
        "https://github.com/OmniNode-ai/omnidash/pull/412",
        "TERMINAL 2026-10-09T01:02:03Z",
        "RULING 2026-10-09T01:02:03Z",
    ],
)
def test_open_asks_close_on_a_row_with_evidence(evidence: str) -> None:
    closing = f"2026-10-09T12:00:00Z | TERMINAL | lane=fix-dash | friction=none | closes-ask=ask-0123456789 | evidence={evidence} | done"
    folded = HandlerOpenAsks().handle(
        ModelOpenAsksRequest(rows=(ASK_ROW, closing), now=NOW)
    )
    assert folded.open_asks == ()
    assert folded.closed == 1


@pytest.mark.parametrize("evidence", ["", "done", "fixed it", "OMN-20905"])
def test_open_asks_stay_open_on_a_closing_row_without_evidence(evidence: str) -> None:
    closing = f"2026-10-09T12:00:00Z | STATUS | lane=fix-dash | closes-ask=ask-0123456789 | evidence={evidence}"
    folded = HandlerOpenAsks().handle(
        ModelOpenAsksRequest(rows=(ASK_ROW, closing), now=NOW)
    )
    assert [a.ask_id for a in folded.open_asks] == ["ask-0123456789"]
    assert not has_closing_evidence(evidence)


def test_open_asks_drop_only_by_operator_capture_and_not_before_the_ask() -> None:
    drop = '2026-10-09T12:00:00Z | STATUS | lane=operator-capture | closes-ask=ask-0123456789 | state=dropped | evidence=operator-drop | "drop ask-0123456789"'
    lane_drop = "2026-10-09T12:00:00Z | STATUS | lane=some-lane | closes-ask=ask-0123456789 | state=dropped"
    early = "2026-10-07T12:00:00Z | TERMINAL | lane=x | friction=none | closes-ask=ask-0123456789 | evidence=omnidash#1"
    assert (
        HandlerOpenAsks()
        .handle(ModelOpenAsksRequest(rows=(ASK_ROW, drop), now=NOW))
        .dropped
        == 1
    )
    assert (
        len(
            HandlerOpenAsks()
            .handle(ModelOpenAsksRequest(rows=(ASK_ROW, lane_drop), now=NOW))
            .open_asks
        )
        == 1
    )
    assert (
        len(
            HandlerOpenAsks()
            .handle(ModelOpenAsksRequest(rows=(early, ASK_ROW), now=NOW))
            .open_asks
        )
        == 1
    )


def test_digest_lists_open_asks_with_age_and_overdue_mark() -> None:
    folded = HandlerOpenAsks().handle(
        ModelOpenAsksRequest(rows=(ASK_ROW, ASK_ROW_2), now=NOW)
    )
    lines = folded.digest.splitlines()
    assert lines[0] == "Open operator asks: 2, 1 waiting over 24h (oldest first)."
    assert lines[1] == "- ask-0123456789 (2d OVERDUE): Fix the dashboard projection."
    assert lines[2] == "- ask-abcdefabcd (2h): Check the h201 floor."
    assert "closes-ask=<id>" in lines[-1]
    assert [a.ask_id for a in folded.overdue] == ["ask-0123456789"]


def test_digest_with_no_open_asks() -> None:
    assert HandlerOpenAsks().handle(ModelOpenAsksRequest(rows=(), now=NOW)).digest == (
        "Open operator asks: none."
    )


# -- drift ------------------------------------------------------------------------------------

RULING_SAME = (
    "2026-09-22T20:35:26Z | RULING | lane=m4-board-rescope | ticket=OMN-1 | question=Where does cloud "
    'work go? | kind=process | "No cloud work in M4; cloud-plane criteria leave M4 for M4.5."'
)
RULING_OPPOSITE = (
    "2026-09-30T10:00:00Z | RULING | lane=other | ticket=OMN-2 | question=Scope | kind=decision | "
    '"Cloud work belongs in M4 after all."'
)
RULING_UNRELATED = (
    "2026-10-01T10:00:00Z | RULING | lane=x | ticket=OMN-3 | question=Ports | kind=decision | "
    '"Use port 8085 for the dev lane."'
)


def test_drift_flags_a_re_ruling_with_the_earlier_stamp() -> None:
    drift = HandlerRulingDrift().handle(
        ModelRulingDriftRequest(
            item=DECISION,
            prior=prior_rulings_from_rows([RULING_SAME, RULING_UNRELATED]),
        )
    )
    assert [m.stamp for m in drift.matches] == ["2026-09-22T20:35:26Z"]
    assert drift.matches[0].relation is EnumDriftRelation.REAFFIRMS
    assert set(drift.matches[0].shared_terms) >= {"cloud", "m4"}
    assert not drift.contradicts


def test_drift_flags_a_contradiction_on_opposite_polarity() -> None:
    drift = HandlerRulingDrift().handle(
        ModelRulingDriftRequest(
            item=DECISION, prior=prior_rulings_from_rows([RULING_OPPOSITE])
        )
    )
    assert drift.contradicts
    assert drift.matches[0].stamp == "2026-09-30T10:00:00Z"


def test_drift_none_for_a_new_subject_and_reads_captured_decisions() -> None:
    assert (
        HandlerRulingDrift()
        .handle(
            ModelRulingDriftRequest(
                item=DECISION, prior=prior_rulings_from_rows([RULING_UNRELATED])
            )
        )
        .matches
        == ()
    )
    captured = (
        "2026-10-01T09:00:00Z | STATUS | lane=operator-capture | kind=decision | item=cap-1 | "
        'classifier=heuristic | "Keep cloud work out of M4."'
    )
    prior = prior_rulings_from_rows([captured, ASK_ROW])
    assert [p.stamp for p in prior] == ["2026-10-01T09:00:00Z"]
    assert (
        HandlerRulingDrift()
        .handle(ModelRulingDriftRequest(item=DECISION, prior=prior))
        .matches
    )


def test_drift_ignores_a_ruling_recorded_after_the_message_was_said() -> None:
    prior = prior_rulings_from_rows([RULING_SAME])
    late = HandlerRulingDrift().handle(
        ModelRulingDriftRequest(
            item=DECISION,
            prior=prior,
            said_at=datetime(2026, 9, 22, 20, 35, 0, tzinfo=UTC),
        )
    )
    assert late.matches == ()


def test_drift_needs_the_operators_own_words_to_match() -> None:
    abstract = _item(K.DECISION, "of course I do", "fixing things confirmation")
    prior = prior_rulings_from_rows(
        [
            "2026-09-30T16:45:51Z | RULING | lane=x | ticket=OMN-4 | question=Keep fixing things? "
            '| kind=decision | "Do not stop fixing things; confirmation is not needed."'
        ]
    )
    assert (
        HandlerRulingDrift()
        .handle(ModelRulingDriftRequest(item=abstract, prior=prior))
        .matches
        == ()
    )


def test_open_asks_a_correction_withdraws_an_ask_captured_in_error() -> None:
    correction = (
        "2026-10-09T12:00:00Z | CORRECTION | lane=capture-asks | corrects=2026-10-08T10:00:00Z | "
        "closes-ask=ask-0123456789,ask-abcdefabcd | scheduled prompt, not the operator"
    )
    folded = HandlerOpenAsks().handle(
        ModelOpenAsksRequest(rows=(ASK_ROW, correction), now=NOW)
    )
    assert folded.open_asks == ()
    assert folded.dropped == 1


def test_open_asks_age_from_when_the_operator_said_it() -> None:
    backfilled = ASK_ROW_2.replace(
        "session=s2", "session=s2 | said=2026-10-08T09:00:00Z"
    )
    folded = HandlerOpenAsks().handle(ModelOpenAsksRequest(rows=(backfilled,), now=NOW))
    assert folded.open_asks[0].stamp == "2026-10-08T09:00:00Z"
    assert folded.open_asks[0].overdue
