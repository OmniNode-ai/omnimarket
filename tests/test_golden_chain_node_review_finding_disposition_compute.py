# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain test for node_review_finding_disposition_compute (OMN-17427).

Recorded finding dispositions and review passes in; per-repo noise share,
one-round pass share, rounds-to-pass and class-fix requests out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

import omnimarket.nodes.node_review_finding_disposition_compute as _node_pkg
from omnimarket.enums.enum_review_finding_disposition import (
    NOISE_DISPOSITIONS,
    EnumReviewFindingDisposition,
)
from omnimarket.models.review_finding_disposition.model_review_finding_disposition_event import (
    DISPOSITION_TOPIC,
    PASS_TOPIC,
    ModelReviewFindingDispositionEntry,
    ModelReviewFindingDispositionRecorded,
    ModelReviewPassRecorded,
    disposition_id_for,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingResultKind,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingWorkerResult,
)
from omnimarket.nodes.node_review_finding_disposition_compute.handlers.handler_review_finding_disposition import (
    HandlerReviewFindingDispositionCompute,
    class_fix_key_for,
)
from omnimarket.nodes.node_review_finding_disposition_compute.models.model_review_finding_disposition import (
    ModelReviewFindingDispositionInput,
    ModelReviewFindingDispositionOutput,
)

_CONTRACT_PATH = Path(_node_pkg.__file__).parent / "contract.yaml"
_AS_OF = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
_D = EnumReviewFindingDisposition


def _answer(
    pr: int,
    finding_id: str,
    disposition: EnumReviewFindingDisposition,
    *,
    repo: str = "OmniNode-ai/omnimarket",
    finding_class: str = "invalid-python",
    review_round: int = 1,
    age_days: float = 1.0,
) -> ModelReviewFindingDispositionRecorded:
    return ModelReviewFindingDispositionRecorded.build(
        repo=repo,
        pr_number=pr,
        review_round=review_round,
        entry=ModelReviewFindingDispositionEntry(
            finding_id=finding_id,
            finding_class=finding_class,
            disposition=disposition,
        ),
        worker_lane="landing-L1",
        recorded_at=_AS_OF - timedelta(days=age_days),
    )


def _pass(
    pr: int, rounds: int, *, repo: str = "OmniNode-ai/omnimarket", age_days: float = 1.0
) -> ModelReviewPassRecorded:
    return ModelReviewPassRecorded.build(
        repo=repo,
        pr_number=pr,
        rounds_to_pass=rounds,
        recorded_at=_AS_OF - timedelta(days=age_days),
    )


def _run(
    dispositions: tuple[ModelReviewFindingDispositionRecorded, ...] = (),
    passes: tuple[ModelReviewPassRecorded, ...] = (),
    **kwargs: int,
) -> ModelReviewFindingDispositionOutput:
    return HandlerReviewFindingDispositionCompute().handle(
        ModelReviewFindingDispositionInput(
            correlation_id=uuid4(),
            as_of=_AS_OF,
            dispositions=dispositions,
            passes=passes,
            **kwargs,
        )
    )


@pytest.mark.unit
def test_contract_declares_fold_topics_and_recorded_inputs() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.review-finding-disposition-folded.v1"
    ]
    produced = {t["topic"] for t in contract["externally_produced_topics"]}
    assert produced == {DISPOSITION_TOPIC, PASS_TOPIC}


@pytest.mark.unit
def test_noise_share_counts_wrong_and_not_applicable() -> None:
    assert {_D.WRONG, _D.NOT_APPLICABLE} == NOISE_DISPOSITIONS
    result = _run(
        (
            _answer(1, "f1", _D.FIXED),
            _answer(1, "f2", _D.WRONG),
            _answer(2, "f3", _D.NOT_APPLICABLE),
            _answer(2, "f4", _D.INTENTIONAL),
        )
    )
    (repo,) = result.repos
    assert (repo.fixed, repo.wrong, repo.intentional, repo.not_applicable) == (
        1,
        1,
        1,
        1,
    )
    assert repo.findings_answered == 4
    assert repo.noise_share == pytest.approx(0.5)


@pytest.mark.unit
def test_convergence_shares_and_mean_rounds() -> None:
    result = _run(
        passes=(_pass(1, 0), _pass(2, 1), _pass(3, 2), _pass(4, 5), _pass(5, 7))
    )
    (repo,) = result.repos
    assert repo.prs_passed == 5
    assert repo.one_round_pass_share == pytest.approx(2 / 5)
    assert repo.five_plus_round_share == pytest.approx(2 / 5)
    assert repo.mean_rounds_to_pass == pytest.approx(3.0)
    assert repo.findings_answered == 0
    assert repo.noise_share is None


@pytest.mark.unit
def test_metrics_are_per_repo_and_sorted() -> None:
    result = _run(
        (
            _answer(1, "f1", _D.WRONG, repo="OmniNode-ai/omnimarket"),
            _answer(9, "f9", _D.FIXED, repo="OmniNode-ai/omniclaude"),
        )
    )
    assert [r.repo for r in result.repos] == [
        "OmniNode-ai/omniclaude",
        "OmniNode-ai/omnimarket",
    ]
    assert [r.noise_share for r in result.repos] == [0.0, 1.0]


@pytest.mark.unit
def test_three_prs_in_the_window_file_one_class_fix_request() -> None:
    result = _run(
        (
            _answer(1, "a", _D.WRONG),
            _answer(2, "b", _D.WRONG),
            _answer(2, "c", _D.NOT_APPLICABLE),
            _answer(3, "d", _D.FIXED),
            _answer(4, "e", _D.FIXED, finding_class="other-class"),
        )
    )
    (request,) = result.class_fix_requests
    assert request.finding_class == "invalid-python"
    assert request.pr_refs == (
        "OmniNode-ai/omnimarket#1",
        "OmniNode-ai/omnimarket#2",
        "OmniNode-ai/omnimarket#3",
    )
    assert request.finding_ids == ("a", "b", "c", "d")
    assert (request.wrong, request.not_applicable, request.fixed) == (2, 1, 1)
    assert request.dedupe_key == class_fix_key_for("invalid-python")
    assert request.window_end - request.window_start == timedelta(days=7)


@pytest.mark.unit
def test_two_prs_or_one_pr_with_many_findings_do_not_recur() -> None:
    assert not _run(
        (_answer(1, "a", _D.WRONG), _answer(2, "b", _D.WRONG))
    ).class_fix_requests
    many = tuple(_answer(1, f"f{i}", _D.WRONG) for i in range(5))
    assert not _run(many).class_fix_requests


@pytest.mark.unit
def test_answers_outside_the_window_do_not_count() -> None:
    result = _run(
        (
            _answer(1, "a", _D.WRONG),
            _answer(2, "b", _D.WRONG),
            _answer(3, "c", _D.WRONG, age_days=8),
        )
    )
    assert not result.class_fix_requests
    assert result.dispositions_counted == 2
    wider = _run(
        (
            _answer(1, "a", _D.WRONG),
            _answer(2, "b", _D.WRONG),
            _answer(3, "c", _D.WRONG, age_days=8),
        ),
        window_days=14,
    )
    assert len(wider.class_fix_requests) == 1


@pytest.mark.unit
def test_class_recurs_across_repos() -> None:
    result = _run(
        (
            _answer(1, "a", _D.WRONG, repo="OmniNode-ai/omnimarket"),
            _answer(1, "b", _D.WRONG, repo="OmniNode-ai/omniclaude"),
            _answer(1, "c", _D.WRONG, repo="OmniNode-ai/omnibase_core"),
        )
    )
    (request,) = result.class_fix_requests
    assert len(request.pr_refs) == 3


@pytest.mark.unit
def test_redelivery_and_changed_answer_count_once_latest_wins() -> None:
    first = _answer(1, "a", _D.WRONG, age_days=2)
    changed = _answer(1, "a", _D.FIXED, age_days=1)
    result = _run((first, first, changed))
    (repo,) = result.repos
    assert repo.findings_answered == 1
    assert (repo.fixed, repo.wrong) == (1, 0)
    assert _run((changed, first)).repos == result.repos


@pytest.mark.unit
def test_same_finding_in_later_round_is_a_separate_answer() -> None:
    result = _run(
        (
            _answer(1, "a", _D.FIXED, review_round=1),
            _answer(1, "a", _D.WRONG, review_round=2),
        )
    )
    assert result.repos[0].findings_answered == 2


@pytest.mark.unit
def test_fold_is_order_independent() -> None:
    rows = tuple(_answer(pr, f"f{pr}", _D.WRONG) for pr in range(1, 6))
    forward = _run(rows, (_pass(1, 1), _pass(2, 3)))
    backward = _run(tuple(reversed(rows)), (_pass(2, 3), _pass(1, 1)))
    assert forward.model_dump() | {"correlation_id": None} == backward.model_dump() | {
        "correlation_id": None
    }


@pytest.mark.unit
def test_empty_input_yields_no_repos_and_no_requests() -> None:
    result = _run()
    assert result.repos == ()
    assert result.class_fix_requests == ()
    assert result.dispositions_counted == 0


@pytest.mark.unit
def test_event_refuses_a_forged_disposition_id() -> None:
    good = _answer(1, "a", _D.WRONG)
    with pytest.raises(ValidationError, match="disposition_id"):
        ModelReviewFindingDispositionRecorded(
            **{
                **good.model_dump(),
                "disposition_id": disposition_id_for("x/y", 2, 1, "a"),
            }
        )


@pytest.mark.unit
def test_entry_refuses_an_unknown_disposition_and_bad_class_slug() -> None:
    with pytest.raises(ValidationError):
        ModelReviewFindingDispositionEntry(
            finding_id="f1",
            finding_class="ok",
            disposition=cast("EnumReviewFindingDisposition", "meh"),
        )
    with pytest.raises(ValidationError):
        ModelReviewFindingDispositionEntry(
            finding_id="f1", finding_class="Has Spaces", disposition=_D.FIXED
        )


@pytest.mark.unit
def test_worker_result_carries_dispositions_and_defaults_to_none() -> None:
    base = {
        "lease_id": 1,
        "pr": "OmniNode-ai/omnimarket#4063",
        "kind": EnumLandingResultKind.FIX_SUBMITTED,
    }
    assert ModelLandingWorkerResult(**base).finding_dispositions == ()
    result = ModelLandingWorkerResult(
        **base,
        finding_dispositions=(
            ModelReviewFindingDispositionEntry(
                finding_id="hr-1", finding_class="invalid-python", disposition=_D.WRONG
            ),
        ),
    )
    assert result.finding_dispositions[0].disposition is _D.WRONG
