# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The operator's words become a RULING, a consent, or an orchestrator inbox MSG (OMN-19513).

Two fixtures are the operator's words as the capture recorded them on 2026-10-18: the reviewer
words said at 03:24:53Z (two items, one reaffirming and one new decision, classified by the
heuristic fallback at 0.50) and the key approval said about 12:30Z, which became a consent only
when the orchestrator wrote it by hand. The assistant replies they answered were not captured on
any row, so each ``*_REPLY`` is a stand-in for the joined reply, not a quote; only the
operator's words are asserted verbatim.

Selectors, each with its control:

* ``reviewer_words``: joined to their question and classified at or above the bar, the words
  become one RULING with the words verbatim and the session link. Controls: the same words with
  no reply go to the inbox; as recorded (heuristic, 0.50) they go to the inbox, not a ruling.
* ``key_approval``: the key words answer a consent proposal but are not a plain yes, so they go
  to the inbox. Control: a plain yes to the same proposal becomes an OPERATOR-CONSENT whose scope
  lists are copied from the reply.
* ``nothing_to_promote``: a status question yields no row.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.models.operator_capture import (
    EnumClassifierSource,
    EnumDriftRelation,
    EnumPromotionVerdict,
    EnumPromptOrigin,
    EnumUtteranceKind,
    ModelDriftMatch,
    ModelRulingDrift,
    ModelUtterance,
    ModelUtteranceClassification,
    ModelUtteranceItem,
    ModelWordsPromotionRequest,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_words_promotion import (
    HandlerWordsPromotion,
    question_of,
)

pytestmark = pytest.mark.unit

K = EnumUtteranceKind
SESSION = "4fb6b47e-ec68-4a15-ab71-0de49ccbcec6"
STAMP = datetime(2026, 10, 19, 3, 0, 0, tzinfo=UTC)
LANE = "orchestrator-bb1a"

REVIEWER_REPLY = (
    "The Claude voters now run beside the two lab voters.\n\n"
    "Keep the Claude reviewers only as a fallback path behind the lab voters, or make them the "
    "main reviewers?"
)
KEY_REPLY = (
    "The Sonnet voter needs an Anthropic key on each lab host.\n"
    "APPROVED SCOPE: register the existing company Anthropic API keys in each lab host local "
    "secret store for the Hostile Reviewer Sonnet voter\n"
    "OUT OF SCOPE: creating or rotating any key; printing any key; production\n"
    "May I register the existing company keys for the Sonnet voter?"
)


def _item(
    item_id: str, kind: EnumUtteranceKind, quote: str, confidence: float
) -> ModelUtteranceItem:
    return ModelUtteranceItem(
        item_id=item_id, kind=kind, quote=quote, subject="s", confidence=confidence
    )


def _request(
    items: tuple[ModelUtteranceItem, ...],
    reply: str | None,
    *,
    capture_id: str,
    said: datetime,
    classifier: EnumClassifierSource = EnumClassifierSource.HEURISTIC,
    drift: dict[str, ModelRulingDrift] | None = None,
) -> ModelWordsPromotionRequest:
    utterance = ModelUtterance(
        capture_id=capture_id,
        text=" ".join(i.quote for i in items),
        session_id=SESSION,
        source="bus:claude",
        captured_at=said,
    )
    return ModelWordsPromotionRequest(
        utterance=utterance,
        classification=ModelUtteranceClassification(
            capture_id=capture_id,
            origin=EnumPromptOrigin.OPERATOR,
            classifier=classifier,
            reason="fixture",
            items=items,
        ),
        drift=drift or {},
        reply=reply,
        lane=LANE,
        stamp=STAMP,
    )


REVIEWER_SAID = datetime(2026, 10, 18, 3, 24, 53, tzinfo=UTC)


def _reviewer(confidence: float) -> tuple[ModelUtteranceItem, ...]:
    return (
        _item(
            "cap-a767805257",
            K.PREFERENCE,
            "I said I do not want them as a fallback path.",
            confidence,
        ),
        _item(
            "cap-7b1ae25518",
            K.DECISION,
            "They should be the main fucking reviewers.",
            confidence,
        ),
    )


def _cells(row: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for cell in row.split(" | ")[2:]:
        key, sep, value = cell.partition("=")
        if sep and key.replace("_", "").replace("-", "").isalnum():
            out.setdefault(key, value)
    return out


def _assert_one_line_row(row: str, row_type: str) -> None:
    assert "\n" not in row
    parts = row.split(" | ")
    assert parts[0] == "2026-10-19T03:00:00Z"
    assert parts[1] == row_type


def test_reviewer_words_ruling() -> None:
    contradicted = ModelRulingDrift(
        matches=(
            ModelDriftMatch(
                stamp="2026-10-12T05:03:28Z",
                lane="orchestrator-1d5b",
                relation=EnumDriftRelation.CONTRADICTS,
                shared_terms=("fallback",),
            ),
        )
    )
    result = HandlerWordsPromotion().handle(
        _request(
            _reviewer(0.9),
            REVIEWER_REPLY,
            capture_id="e0973ef1067e73b6",
            said=REVIEWER_SAID,
            classifier=EnumClassifierSource.DELEGATED,
            drift={"cap-7b1ae25518": contradicted},
        )
    )
    assert result.verdict is EnumPromotionVerdict.RULING
    (row,) = result.rows
    _assert_one_line_row(row, "RULING")
    cells = _cells(row)
    assert cells["lane"] == LANE
    assert cells["question"] == (
        "Keep the Claude reviewers only as a fallback path behind the lab voters, or make them "
        "the main reviewers?"
    )
    assert cells["session"] == SESSION
    assert cells["capture"] == "e0973ef1067e73b6"
    assert cells["said"] == "2026-10-18T03:24:53Z"
    assert cells["promoted_from"] == "cap-a767805257,cap-7b1ae25518"
    assert cells["amends"] == "2026-10-12T05:03:28Z"
    assert row.endswith(
        '"I said I do not want them as a fallback path. / They should be the main fucking '
        'reviewers."'
    )


def test_reviewer_words_control_no_reply_goes_to_inbox() -> None:
    result = HandlerWordsPromotion().handle(
        _request(
            _reviewer(0.9), None, capture_id="e0973ef1067e73b6", said=REVIEWER_SAID
        )
    )
    assert result.verdict is EnumPromotionVerdict.INBOX
    (row,) = result.rows
    _assert_one_line_row(row, "MSG")
    cells = _cells(row)
    assert cells["from"] == "operator-promotion"
    assert cells["id"] == "2026-10-19T03:00:00Z-operator-promotion"
    assert cells["to"] == LANE
    assert "They should be the main fucking reviewers." in row
    assert "no joined question" in result.reason


def test_reviewer_words_control_as_recorded_heuristic_goes_to_inbox() -> None:
    result = HandlerWordsPromotion().handle(
        _request(
            _reviewer(0.5),
            REVIEWER_REPLY,
            capture_id="e0973ef1067e73b6",
            said=REVIEWER_SAID,
        )
    )
    assert result.verdict is EnumPromotionVerdict.INBOX
    assert "below 0.80" in result.reason
    assert "question=Keep the Claude reviewers" in result.rows[0]


KEY_SAID = datetime(2026, 10, 18, 12, 30, 0, tzinfo=UTC)


def test_key_approval_goes_to_inbox() -> None:
    items = (
        _item(
            "cap-k1",
            K.DECISION,
            "we have like a bunch of keys for reviewers. Like 4 or 5 of them, one for each account.",
            0.9,
        ),
        _item("cap-k2", K.DECISION, "I already have api keys, remember?", 0.9),
    )
    result = HandlerWordsPromotion().handle(
        _request(items, KEY_REPLY, capture_id="key-approval", said=KEY_SAID)
    )
    assert result.verdict is EnumPromotionVerdict.INBOX
    (row,) = result.rows
    _assert_one_line_row(row, "MSG")
    assert "I already have api keys, remember?" in row
    assert "one for each account." in row
    for marker in ("APPROVED SCOPE", "OUT OF SCOPE", "OPERATOR-CONSENT"):
        assert marker not in row
    assert "not a plain yes" in result.reason


def test_key_approval_control_plain_yes_is_consent() -> None:
    items = (_item("cap-y", K.DECISION, "Yes, go ahead.", 0.5),)
    result = HandlerWordsPromotion().handle(
        _request(items, KEY_REPLY, capture_id="yes", said=KEY_SAID)
    )
    assert result.verdict is EnumPromotionVerdict.OPERATOR_CONSENT
    (row,) = result.rows
    _assert_one_line_row(row, "OPERATOR-CONSENT")
    assert '| "Yes, go ahead." |' in row
    assert (
        "| APPROVED SCOPE: register the existing company Anthropic API keys in each lab host "
        "local secret store for the Hostile Reviewer Sonnet voter |"
    ) in row
    assert row.endswith(
        "| OUT OF SCOPE: creating or rotating any key; printing any key; production"
    )
    assert (
        _cells(row)["question"]
        == "May I register the existing company keys for the Sonnet voter?"
    )


def test_consent_proposal_missing_a_scope_list_is_never_consent() -> None:
    reply = "APPROVED SCOPE: register the keys\nMay I register the keys?"
    items = (_item("cap-y", K.DECISION, "Yes.", 0.9),)
    result = HandlerWordsPromotion().handle(
        _request(items, reply, capture_id="y", said=KEY_SAID)
    )
    assert result.verdict is EnumPromotionVerdict.INBOX
    assert "both scope lists" in result.reason


def test_nothing_to_promote_status_question() -> None:
    items = (
        _item(
            "cap-12af9d8d40",
            K.STATUS_QUESTION,
            "Is there any value in keeping Quen as co-reviewers?",
            0.5,
        ),
    )
    result = HandlerWordsPromotion().handle(
        _request(
            items, REVIEWER_REPLY, capture_id="ce812f9bf0d4daa7", said=REVIEWER_SAID
        )
    )
    assert result.verdict is EnumPromotionVerdict.NONE
    assert result.rows == ()


def test_question_is_the_last_question_sentence_as_written() -> None:
    assert (
        question_of("Done. Merge it now? Or wait for the train?", 400)
        == "Or wait for the train?"
    )
    assert question_of("All done.", 400) is None
    assert question_of(None, 400) is None


def test_words_with_pipes_and_quotes_stay_one_row() -> None:
    items = (_item("cap-q", K.DECISION, 'Use "main" | not fallback', 0.9),)
    result = HandlerWordsPromotion().handle(
        _request(items, REVIEWER_REPLY, capture_id="q", said=REVIEWER_SAID)
    )
    (row,) = result.rows
    assert "verbatim=normalized" in row
    assert row.endswith("\"Use ''main'' ¦ not fallback\"")
