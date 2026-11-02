# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recall, contradiction, mention and precision scored from recorded answers."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from omnibase_core.nodes.node_compute import NodeCompute
from pydantic import ValidationError

from omnimarket.nodes.node_compaction_score_compute.handlers.handler_score import (
    HandlerCompactionScore,
    check_control,
)
from omnimarket.nodes.node_compaction_score_compute.models.model_score import (
    EnumControl,
    EnumItemKind,
    EnumStratum,
    EnumVerdict,
    ModelGroundTruthItem,
    ModelLedgerPrefix,
    ModelRecordedAnswer,
    ModelScoreRequest,
    ModelScoreResult,
)
from omnimarket.nodes.node_compaction_score_compute.node import (
    NodeCompactionScoreCompute,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE = ROOT / "src/omnimarket/nodes/node_compaction_score_compute"
DIGEST = "sha256:manifest-1"
BOUNDARY = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)

QUOTE_A = "we should count the savings as net saved context after every compaction"
QUOTE_B = "stay inside the approved scope lists and spend nothing outside them"
STAMP_A = "2026-10-15T08:07:56Z"
STAMP_B = "2026-10-20T10:00:00Z"

RULING = ModelGroundTruthItem(
    key=STAMP_A,
    kind=EnumItemKind.RULING,
    question="How are savings counted?",
    quote=QUOTE_A,
)
CONSENT = ModelGroundTruthItem(
    key=STAMP_B,
    kind=EnumItemKind.OPERATOR_CONSENT,
    question="What scope was approved?",
    quote=QUOTE_B,
)
CLAIM = ModelGroundTruthItem(key="fold-lane-7", kind=EnumItemKind.OPEN_CLAIM)
ASK = ModelGroundTruthItem(
    key="qkey-releases",
    kind=EnumItemKind.OPEN_ASK,
    names=("qkey-releases", "releases ask"),
)
PLANTED = ModelGroundTruthItem(
    key="planted-1",
    kind=EnumItemKind.RULING,
    stratum=EnumStratum.PLANTED,
    question="What does token ZX-91 mean?",
    quote="token ZX-91 means rebuild the index before every merge",
)
PREFIX = ModelLedgerPrefix(
    tickets=("OMN-21288", "OMN-21991"),
    pull_requests=("OmniNode-ai/omnimarket#4838",),
    lanes=("fold-lane-7", "done-lane-2"),
    shas=("b700a6772d46eab64a8710ecdc69b08f09be6001",),
    paths=("src/omnimarket/nodes/node_compaction_probe_compute/node.py",),
    timestamps=(STAMP_A, STAMP_B, "2026-10-01T00:00:00Z", "2026-10-21T00:00:00Z"),
    closed_lanes=("done-lane-2",),
    superseded_rulings=("2026-10-01T00:00:00Z",),
    amended_rulings={STAMP_A: ("2026-10-21T00:00:00Z",)},
)


def ans(
    key: str,
    text: str,
    *,
    repeat: int = 0,
    attempt: int = 0,
    judged: EnumVerdict | None = None,
    available: bool = True,
) -> ModelRecordedAnswer:
    return ModelRecordedAnswer(
        item_key=key,
        repeat=repeat,
        attempt=attempt,
        manifest_digest=DIGEST,
        text=text,
        judged=judged,
        available=available,
    )


def request(
    items: tuple[ModelGroundTruthItem, ...],
    answers: tuple[ModelRecordedAnswer, ...] = (),
    *,
    arm: str | None = None,
    context: str = "",
    probe: tuple[ModelRecordedAnswer, ...] = (),
    repeats: int = 3,
) -> ModelScoreRequest:
    return ModelScoreRequest(
        case_id="case-1",
        arm=arm,
        boundary_at=BOUNDARY,
        manifest_digest=DIGEST,
        repeats=repeats,
        items=items,
        answers=answers,
        context_text=context,
        open_probe_answers=probe,
        ledger=PREFIX,
    )


def score(req: ModelScoreRequest) -> ModelScoreResult:
    return HandlerCompactionScore().handle(req)


def all_repeats(
    items: tuple[ModelGroundTruthItem, ...],
    text: str,
    judged: EnumVerdict | None = None,
    repeats: int = 3,
) -> tuple[ModelRecordedAnswer, ...]:
    return tuple(
        ans(item.key, text, repeat=r, judged=judged)
        for item in items
        for r in range(repeats)
    )


# --- S1: each verdict ------------------------------------------------------------------------


def test_citing_the_stamp_is_recalled_lexically() -> None:
    result = score(
        request((RULING,), all_repeats((RULING,), f"per the ruling of {STAMP_A}"))
    )
    assert result.ledger_recall.judged.recalled == 3
    assert result.ledger_recall.lexical_recalled == 3


def test_five_consecutive_quote_words_is_recalled_lexically() -> None:
    text = "The operator said we should count the savings as net saved context."
    result = score(request((RULING,), all_repeats((RULING,), text)))
    assert result.ledger_recall.recall == 1.0


def test_four_quote_words_are_not_enough() -> None:
    result = score(request((RULING,), all_repeats((RULING,), "we should count the")))
    assert result.ledger_recall.judged.recalled == 0
    assert result.ledger_recall.judged.absent == 3


def test_no_answer_and_unknown_answers_are_absent() -> None:
    answers = (
        ans(RULING.key, "", repeat=0),
        ans(RULING.key, "I do not know.", repeat=1, judged=EnumVerdict.ABSENT),
        ans(RULING.key, "no idea", repeat=2),
    )
    result = score(request((RULING,), answers))
    assert result.ledger_recall.judged.absent == 3
    assert result.ledger_recall.judged.contradicted == 0


def test_judge_verdict_decides_meaning_beyond_the_lexical_reading() -> None:
    answers = all_repeats(
        (RULING,), "savings count towards net context", EnumVerdict.RECALLED
    )
    result = score(request((RULING,), answers))
    assert result.ledger_recall.judged.recalled == 3
    assert result.ledger_recall.lexical_recalled == 0
    assert result.ledger_recall.lexical_recall == 0.0


def test_absent_contradicted_apart() -> None:
    answers = (
        ans(RULING.key, "", repeat=0),
        ans(
            RULING.key,
            "savings are not counted",
            repeat=1,
            judged=EnumVerdict.CONTRADICTED,
        ),
        ans(
            RULING.key,
            "savings are not counted",
            repeat=2,
            judged=EnumVerdict.CONTRADICTED,
        ),
    )
    result = score(request((RULING,), answers))
    counts = result.ledger_recall.judged
    assert (counts.absent, counts.contradicted) == (1, 2)
    assert result.ledger_recall.contradiction == pytest.approx(2 / 3)
    assert result.ledger_recall.recall == 0.0
    # Merging the two verdicts would leave one number; the apart counts differ.
    assert counts.absent != counts.contradicted
    assert result.ledger_recall.item_verdicts[RULING.key] == (
        EnumVerdict.ABSENT,
        EnumVerdict.CONTRADICTED,
        EnumVerdict.CONTRADICTED,
    )


def test_unavailable_stays_in_the_denominator_and_never_passes() -> None:
    answers = (
        ans(RULING.key, f"{STAMP_A}", repeat=0),
        ans(RULING.key, "", repeat=1, available=False),
    )
    result = score(request((RULING,), answers, repeats=2))
    assert result.ledger_recall.judged.unavailable == 1
    assert result.ledger_recall.trials == 2
    assert result.ledger_recall.recall == 0.5


def test_a_missing_repeat_is_unavailable_not_skipped() -> None:
    result = score(request((RULING,), (ans(RULING.key, STAMP_A, repeat=0),)))
    assert result.ledger_recall.judged.unavailable == 2
    assert result.ledger_recall.recall == pytest.approx(1 / 3)


def test_latest_valid_attempt_per_key_wins_and_earlier_ones_are_kept() -> None:
    answers = (
        ans(RULING.key, "", repeat=0, attempt=0),
        ans(RULING.key, STAMP_A, repeat=0, attempt=1),
        ans(RULING.key, "", repeat=0, attempt=2, available=False),
    )
    result = score(request((RULING,), answers, repeats=1))
    assert result.ledger_recall.judged.recalled == 1
    assert result.ledger_recall.judged.unavailable == 0


def test_answer_under_another_manifest_digest_is_refused() -> None:
    bad = ModelRecordedAnswer(
        item_key=RULING.key, repeat=0, manifest_digest="sha256:other", text="x"
    )
    with pytest.raises(ValidationError):
        request((RULING,), (bad,))


def test_planted_items_are_their_own_stratum_never_pooled() -> None:
    answers = (
        *all_repeats((RULING,), STAMP_A),
        *all_repeats((PLANTED,), "no idea", EnumVerdict.CONTRADICTED),
    )
    result = score(request((RULING, PLANTED), answers))
    assert result.ledger_recall.items == 1
    assert result.ledger_recall.recall == 1.0
    assert result.planted_recall.items == 1
    assert result.planted_recall.contradiction == 1.0
    assert result.planted_recall.recall == 0.0


def test_planted_open_loop_item_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelGroundTruthItem(
            key="p", kind=EnumItemKind.OPEN_CLAIM, stratum=EnumStratum.PLANTED
        )


# --- S2: mention ------------------------------------------------------------------------------


def test_mention_reads_the_context_or_the_open_probe() -> None:
    probe = (ans(CLAIM.key, "Open: the releases ask is still pending"),)
    result = score(
        request(
            (CLAIM, ASK),
            context="Lane fold-lane-7 is still running.",
            probe=probe,
        )
    )
    assert result.mention.mentioned == 2
    assert result.mention.mention == 1.0


def test_mention_misses_are_listed() -> None:
    result = score(request((CLAIM, ASK), context="Nothing relevant here."))
    assert result.mention.mentioned == 0
    assert result.mention.unmentioned == (CLAIM.key, ASK.key)


def test_mention_needs_the_whole_key_not_a_fragment() -> None:
    result = score(request((CLAIM,), context="the fold-lane-77 lane is running"))
    assert result.mention.mentioned == 0


def test_unavailable_open_probe_cannot_establish_a_mention() -> None:
    probe = (ans(CLAIM.key, "fold-lane-7 is open", available=False),)
    result = score(request((CLAIM,), context="", probe=probe))
    assert result.mention.mentioned == 0


def test_empty_open_loop_set_has_unknown_mention() -> None:
    assert score(request((RULING,))).mention.mention is None


# --- S4: precision ----------------------------------------------------------------------------


def test_false_memory_invented_ticket_id() -> None:
    result = score(request((RULING,), context="Tracked under OMN-99999 and OMN-21288."))
    assert result.precision.false_memories == ("OMN-99999",)


def test_false_memory_covers_every_identifier_family() -> None:
    context = (
        "PR OmniNode-ai/omnimarket#9999 at 2026-01-01T00:00:00Z sha deadbeef1234 "
        "in src/omnimarket/nodes/node_made_up/node.py by lane=ghost-lane."
    )
    result = score(request((RULING,), context=context))
    assert set(result.precision.false_memories) == {
        "OmniNode-ai/omnimarket#9999",
        "2026-01-01T00:00:00Z",
        "deadbeef1234",
        "src/omnimarket/nodes/node_made_up/node.py",
        "ghost-lane",
    }


def test_identifiers_found_in_the_prefix_are_not_false_memories() -> None:
    context = (
        "OMN-21991 OmniNode-ai/omnimarket#4838 b700a6772d46 "
        "src/omnimarket/nodes/node_compaction_probe_compute/node.py "
        f"{STAMP_A} lane=fold-lane-7"
    )
    result = score(request((RULING,), context=context))
    assert result.precision.false_memories == ()
    assert result.precision.identifiers_checked == 6


def test_stale_memory_lane_stated_open_that_the_ledger_closed() -> None:
    result = score(request((RULING,), context="Lane done-lane-2 is still running."))
    [stale] = result.precision.stale_memories
    assert (stale.reason, stale.identifier) == ("lane_closed", "done-lane-2")


def test_lane_stated_closed_is_not_stale() -> None:
    result = score(request((RULING,), context="Lane done-lane-2 is closed and merged."))
    assert result.precision.stale_memories == ()


def test_stale_memory_superseded_ruling_stated_in_force() -> None:
    context = "Ruling 2026-10-01T00:00:00Z is in force."
    result = score(request((RULING,), context=context))
    assert [s.reason for s in result.precision.stale_memories] == ["ruling_superseded"]
    assert result.precision.false_memories == ()


def test_amended_ruling_is_stale_only_when_quoted_as_complete_without_its_amender() -> (
    None
):
    complete = score(
        request((RULING,), context=f"Ruling {STAMP_A} is the complete rule.")
    )
    assert [s.reason for s in complete.precision.stale_memories] == ["amender_missing"]
    with_amender = score(
        request(
            (RULING,),
            context=f"Ruling {STAMP_A} is the complete rule; amended by 2026-10-21T00:00:00Z.",
        )
    )
    assert with_amender.precision.stale_memories == ()
    plain = score(request((RULING,), context=f"Ruling {STAMP_A} is in force."))
    assert plain.precision.stale_memories == ()


# --- controls 2 to 4 --------------------------------------------------------------------------

LEDGER_ITEMS = (RULING, CONSENT)


def oracle_request() -> ModelScoreRequest:
    texts = {RULING.key: QUOTE_A, CONSENT.key: QUOTE_B}
    answers = tuple(
        ans(item.key, texts[item.key], repeat=r, judged=EnumVerdict.RECALLED)
        for item in LEDGER_ITEMS
        for r in range(3)
    )
    return request(LEDGER_ITEMS, answers, arm="oracle")


def many(n: int) -> tuple[ModelGroundTruthItem, ...]:
    return tuple(
        ModelGroundTruthItem(
            key=f"2026-10-{i + 1:02d}T00:00:00Z",
            kind=EnumItemKind.RULING,
            quote=f"quote {i}",
        )
        for i in range(n)
    )


def test_oracle_control_passes_with_enough_trials() -> None:
    items = many(14)
    answers = tuple(
        ans(i.key, i.quote, repeat=r, judged=EnumVerdict.RECALLED)
        for i in items
        for r in range(3)
    )
    result = score(request(items, answers, arm="oracle"))
    check = check_control(EnumControl.ORACLE, result)
    assert check.passed, check.failures
    assert result.ledger_recall.recall_wilson_lower is not None
    assert result.ledger_recall.recall_wilson_lower >= 0.90


def test_oracle_control_fails_on_too_few_trials_for_the_wilson_bound() -> None:
    result = score(oracle_request())
    check = check_control(EnumControl.ORACLE, result)
    assert not check.passed
    assert any("wilson" in failure for failure in check.failures)


def test_oracle_control_fails_on_any_contradiction() -> None:
    items = many(14)
    answers = tuple(
        ans(i.key, i.quote, repeat=r, judged=EnumVerdict.RECALLED)
        for i in items
        for r in range(3)
    )
    answers = (
        *answers[:-1],
        ans(items[-1].key, "x", repeat=2, attempt=1, judged=EnumVerdict.CONTRADICTED),
    )
    check = check_control(EnumControl.ORACLE, score(request(items, answers)))
    assert not check.passed
    assert any("contradiction" in failure for failure in check.failures)


def test_empty_control_scores_near_zero_and_fails_when_it_answers() -> None:
    empty = score(
        request(
            (RULING, CLAIM),
            all_repeats((RULING, CLAIM), "", None),
            context="",
        )
    )
    assert check_control(EnumControl.EMPTY, empty).passed
    leaking = score(request((RULING, CLAIM), all_repeats((RULING, CLAIM), STAMP_A)))
    assert not check_control(EnumControl.EMPTY, leaking).passed


def test_inverted_control_needs_ninety_percent_contradicted() -> None:
    items = many(10)
    contradicted = tuple(
        ans(i.key, "the opposite", repeat=r, judged=EnumVerdict.CONTRADICTED)
        for i in items
        for r in range(3)
    )
    assert check_control(
        EnumControl.INVERTED, score(request(items, contradicted))
    ).passed
    mixed = (
        *contradicted[:-4],
        *(ans(items[-1].key, "", repeat=r, attempt=1) for r in range(3)),
        ans(items[-2].key, "", repeat=2, attempt=1),
    )
    check = check_control(EnumControl.INVERTED, score(request(items, mixed)))
    assert not check.passed


# --- arm blindness ----------------------------------------------------------------------------


def _fold_is_arm_blind(handler: HandlerCompactionScore) -> bool:
    base = oracle_request()
    results = [
        handler.handle(base.model_copy(update={"arm": arm}))
        for arm in (None, "oracle", "empty", "inverted", "stock", "mod")
    ]
    return all(r == results[0] for r in results)


def test_score_arm_blind() -> None:
    assert _fold_is_arm_blind(HandlerCompactionScore())


def test_score_arm_blind_detects_a_mutant_that_reads_the_arm_label() -> None:
    class Mutant(HandlerCompactionScore):
        def handle(self, request: ModelScoreRequest) -> ModelScoreResult:
            result = super().handle(request)
            if request.arm == "empty":
                return result.model_copy(update={"case_id": "case-empty"})
            return result

    assert not _fold_is_arm_blind(Mutant())


def test_score_arm_blind_result_carries_no_arm() -> None:
    assert "arm" not in ModelScoreResult.model_fields
    assert "arm" not in str(score(oracle_request()).model_dump())


def test_result_is_deterministic() -> None:
    assert score(oracle_request()) == score(oracle_request())


# --- packaging --------------------------------------------------------------------------------


def test_node_is_a_pure_compute_node_with_no_topics() -> None:
    assert issubclass(NodeCompactionScoreCompute, NodeCompute)
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["descriptor"]["purity"] == "pure"


def test_node_is_registered_under_the_onex_nodes_entry_point() -> None:
    found = {ep.name: ep.value for ep in entry_points(group="onex.nodes")}
    assert (
        found["node_compaction_score_compute"]
        == "omnimarket.nodes.node_compaction_score_compute"
    )
