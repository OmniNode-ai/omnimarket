# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Measurement controls are synthetic; they do not stand in for corpus evaluation."""

import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_orchestrator_promise import (
    ModelPromiseBarRequest,
    ModelPromiseClassifyRequest,
    ModelPromiseLabel,
    ModelPromisePolicy,
)
from omnimarket.models.operator_capture import ModelUtterance
from omnimarket.nodes.node_orchestrator_promise_classify_compute.handlers.handler_promise_bar import (
    HandlerPromiseBar,
)
from omnimarket.nodes.node_orchestrator_promise_classify_compute.handlers.handler_promise_classify import (
    HandlerPromiseClassify,
)

pytestmark = pytest.mark.unit


def sample_request(**updates):
    raw = yaml.safe_load(
        files("omnimarket.nodes.node_orchestrator_promise_classify_compute")
        .joinpath("contract.yaml")
        .read_text()
    )["config"]
    raw.update(minimum_precision=0.9, minimum_recall=0.9)
    raw.update(updates)
    policy = ModelPromisePolicy.model_validate(raw)
    fixture = json.loads(
        Path(__file__)
        .with_name("fixtures")
        .joinpath("labelled_regressions.json")
        .read_text()
    )
    return ModelPromiseBarRequest(
        corpus_id=fixture["corpus_id"],
        origin=fixture["origin"],
        labelled_by=fixture["labelled_by"],
        policy=policy,
        samples=tuple(
            ModelPromiseLabel(
                request=ModelPromiseClassifyRequest(
                    utterance=ModelUtterance(
                        capture_id=item["id"],
                        text=item["text"],
                        session_id="synthetic-session",
                        source="synthetic-fixture",
                        lane="orchestrator-fixture",
                        captured_at=datetime(2026, 11, 4, 12, tzinfo=UTC),
                    ),
                    policy=policy,
                ),
                promise_sentences=item["promise_sentences"],
            )
            for item in fixture["samples"]
        ),
    )


def test_promise_bar_counts_intervals_and_synthetic_cannot_activate() -> None:
    result = HandlerPromiseBar().handle(sample_request())
    assert result.replies == 60
    assert (result.true_positive, result.false_positive, result.false_negative) == (
        40,
        0,
        0,
    )
    assert result.precision.estimate == result.recall.estimate == 1
    assert result.precision.successes == result.precision.total == 40
    assert result.recall.lower == pytest.approx(0.9123783988)
    assert result.precision.upper == pytest.approx(1)
    assert result.interval_confidence == 0.95
    assert not result.passed
    assert result.reasons == (
        "labelled sample is synthetic, not the orchestrator corpus",
    )


def test_promise_bar_positive_control_and_below_bar_falsifier() -> None:
    # Test-only provenance change exercises the gate; it is never persisted as lab evidence.
    request = sample_request().model_copy(update={"origin": "corpus"})
    assert HandlerPromiseBar().handle(request).passed
    wrong = tuple(
        sample.model_copy(
            update={"promise_sentences": (1,) if not sample.promise_sentences else ()}
        )
        for sample in request.samples
    )
    result = HandlerPromiseBar().handle(request.model_copy(update={"samples": wrong}))
    assert not result.passed
    assert (result.true_positive, result.false_positive, result.false_negative) == (
        0,
        40,
        20,
    )
    assert result.precision.estimate == result.recall.estimate == 0
    assert set(result.reasons) == {
        "precision is below the overlay bar",
        "recall is below the overlay bar",
    }


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"minimum_precision": None}, "precision bar is unset"),
        ({"minimum_recall": None}, "recall bar is unset"),
        ({"minimum_precision": 0.99}, "precision is below the overlay bar"),
        ({"minimum_recall": 0.99}, "recall is below the overlay bar"),
    ],
)
def test_promise_bar_overlay_limits_are_binding(updates, reason) -> None:
    result = HandlerPromiseBar().handle(
        sample_request(**updates).model_copy(update={"origin": "corpus"})
    )
    assert not result.passed
    assert reason in result.reasons


def test_promise_bar_duplicate_source_replies_refused() -> None:
    request = sample_request()
    with pytest.raises(ValueError, match="distinct source replies"):
        HandlerPromiseBar().handle(
            request.model_copy(
                update={"samples": (*request.samples, request.samples[0])}
            )
        )


def test_promise_bar_insufficient_sample_remains_shadow() -> None:
    request = sample_request().model_copy(update={"origin": "corpus"})
    report = HandlerPromiseBar().handle(
        request.model_copy(update={"samples": request.samples[:59]})
    )
    assert not report.passed
    assert "labelled sample is smaller than the overlay minimum" in report.reasons


def test_promise_bar_active_requires_matching_measurement_and_keeps_low_confidence() -> (
    None
):
    request = sample_request(mode="active").model_copy(update={"origin": "corpus"})
    report = HandlerPromiseBar().handle(request)
    assert report.passed
    high, low = request.samples[0].request, request.samples[20].request
    for value, state in [(high, "open"), (low, "candidate")]:
        assert (
            HandlerPromiseClassify()
            .handle(value.model_copy(update={"evaluation": report}))
            .items[0]
            .state
            == state
        )
        assert HandlerPromiseClassify().handle(value).items[0].state == "candidate"
    changed = high.model_copy(
        update={
            "policy": high.policy.model_copy(update={"classifier_revision": "changed"})
        }
    )
    assert (
        HandlerPromiseClassify()
        .handle(changed.model_copy(update={"evaluation": report}))
        .items[0]
        .state
        == "candidate"
    )
    shadow = high.model_copy(
        update={
            "policy": high.policy.model_copy(update={"mode": "shadow"}),
            "evaluation": report,
        }
    )
    assert HandlerPromiseClassify().handle(shadow).items[0].state == "candidate"


def test_promise_bar_undefined_precision_and_recall_are_not_success() -> None:
    request = sample_request()
    empty = tuple(
        sample.model_copy(
            update={
                "request": sample.request.model_copy(
                    update={
                        "utterance": sample.request.utterance.model_copy(
                            update={"text": "Thanks."}
                        )
                    }
                ),
                "promise_sentences": (),
            }
        )
        for sample in request.samples
    )
    result = HandlerPromiseBar().handle(
        request.model_copy(update={"samples": empty, "origin": "corpus"})
    )
    assert result.precision.estimate is result.recall.estimate is None
    assert result.precision.lower == result.recall.lower == 0
    assert not result.passed


def test_promise_bar_counts_multiple_items_in_one_source_sentence() -> None:
    request = sample_request()
    first = request.samples[0]
    many = first.model_copy(
        update={
            "request": first.request.model_copy(
                update={
                    "utterance": first.request.utterance.model_copy(
                        update={"text": " and ".join(["I will check it"] * 10) + "."}
                    )
                }
            ),
            "promise_sentences": (1,) * 10,
        }
    )
    report = HandlerPromiseBar().handle(
        request.model_copy(update={"samples": (many, *request.samples[1:])})
    )
    assert report.true_positive == 49
    assert report.false_positive == report.false_negative == 0


def test_promise_bar_open_rows_enter_wave_one_fold_but_candidates_do_not() -> None:
    from omnimarket.models.model_ask_state import ModelAskStateRequest
    from omnimarket.models.model_orchestrator_promise import (
        ModelPromiseCaptureRecord,
        ModelPromiseEffectOverlay,
    )
    from omnimarket.nodes.node_ask_state_compute.handlers.handler_ask_state import (
        HandlerAskState,
    )
    from omnimarket.nodes.node_orchestrator_promise_capture_effect.handlers.handler_promise_capture import (
        HandlerPromiseCapture,
    )
    from omnimarket.nodes.node_orchestrator_promise_capture_effect.handlers.store_promise_capture import (
        StorePromiseCapture,
    )
    from omnimarket.nodes.node_orchestrator_promise_classify_compute.node import (
        NodeOrchestratorPromiseClassifyCompute,
    )

    measured = sample_request(mode="active").model_copy(update={"origin": "corpus"})
    report = HandlerPromiseBar().handle(measured)
    assert report.passed
    handler = HandlerPromiseCapture(
        store=StorePromiseCapture(":memory:"),
        classifier=NodeOrchestratorPromiseClassifyCompute(),
        overlay=ModelPromiseEffectOverlay(
            store_path=":memory:",
            assistant_reply_kind="assistant_reply",
            policy=measured.policy,
        ),
        evaluation=report,
    )
    common = {
        "session_id": "session-a",
        "content_kind": "assistant_reply",
        "lane": "orchestrator-fixture",
        "emitted_at": "2026-11-04T12:00:00Z",
    }
    high = handler.handle(
        ModelPromiseCaptureRecord(
            **common, content_record_id="open-promise", content="I will install it."
        )
    )
    low = handler.handle(
        ModelPromiseCaptureRecord(
            **common,
            content_record_id="candidate-promise",
            content="I might read it back.",
        )
    )
    assert len(high.ledger_rows) == 1
    assert low.ledger_rows == ()
    config = yaml.safe_load(
        files("omnimarket.nodes.node_ask_state_compute")
        .joinpath("contract.yaml")
        .read_text()
    )["config"]
    fold = HandlerAskState().handle(
        ModelAskStateRequest(
            prompts=(),
            rows=high.ledger_rows + low.ledger_rows,
            now=datetime(2026, 11, 4, 13, tzinfo=UTC),
            since=datetime(2026, 11, 4, tzinfo=UTC),
            config=config,
        )
    )
    assert len(fold.asks) == 1
    assert fold.asks[0].kind == "promise"
    assert fold.asks[0].owner_lane == "orchestrator-fixture"
    assert fold.asks[0].words == "I will install it."
