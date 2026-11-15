# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the delegation history fold: from the projection's readings and the reports' tables to the served document.

The golden chain reads three models, two task kinds and one experiment and checks every number by hand-worked arithmetic: Wilson
intervals, nearest-rank percentiles, day sums, the engine kind and the settings each model carries. Each error is a named injected
break, and each turns the reading: a run that never reached a model is no model's attempt, a setting no overlay records stays
unrecorded, an arm rule naming a missing backend is named in the sources, and a broken Wilson formula fails the golden numbers.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.floor_delegation_history import (
    SCHEMA_ID,
    ModelDelegationHistory,
    ModelSeriesPoint,
    ModelSuccess,
)
from omnimarket.nodes.node_floor_delegation_history_compute.handlers import (
    handler_floor_delegation_history as handler_module,
)
from omnimarket.nodes.node_floor_delegation_history_compute.handlers.handler_floor_delegation_history import (
    HandlerFloorDelegationHistory,
    wilson,
)

from .support import LOCAL_HOST, NOW, POLICY, SECOND_HOST, attempts, request, seen

pytestmark = pytest.mark.unit


def _build(**changes: object) -> ModelDelegationHistory:
    return HandlerFloorDelegationHistory().handle(request(**changes))


def _series(doc: ModelDelegationHistory, model: str, task: str):
    return next(s for s in doc.series if s.model == model and s.task_kind == task)


def test_golden_wilson_matches_the_intervals_the_reports_state() -> None:
    low, high = wilson(114, 156)
    assert (round(low, 3), round(high, 3)) == (
        0.656,
        0.794,
    )  # exp-triagepow-6800 states 114/156 = 0.731 [0.656, 0.794]
    low, high = wilson(130, 156)
    assert (round(low, 3), round(high, 3)) == (
        0.767,
        0.884,
    )  # the same report: 130/156 = 0.833 [0.767, 0.884]
    assert wilson(8, 10) == (0.4902, 0.9433)
    assert wilson(0, 0) == (None, None)
    assert wilson(60, 60) == (0.9398, 1.0)


def test_golden_document_has_all_three_sections_filled() -> None:
    doc = _build()
    assert doc.schema_id == SCHEMA_ID == "omnimarket.floor_delegation_history.v1"
    assert doc.generated_at == NOW
    assert doc.window_days == 14
    assert [m.id for m in doc.models] == [
        f"Model-X@{LOCAL_HOST}",
        f"Model-X@{SECOND_HOST}",
        "glm-flash",
    ]
    assert doc.counts.models == 3
    assert doc.counts.experiments == 1
    assert (
        doc.counts.attempts_in_window == 10 + 10 + 4 + 3 + 2
    )  # the 9 "not routed" attempts are no model's
    assert doc.counts.series == len(doc.series)


def test_golden_models_carry_engine_kind_and_settings_as_the_overlays_record_them() -> (
    None
):
    doc = _build()
    by = {m.id: m for m in doc.models}
    first = by[f"Model-X@{LOCAL_HOST}"]
    assert (first.engine_kind, first.host, first.context_tokens, first.thinking) == (
        "local",
        LOCAL_HOST,
        131072,
        "on",
    )
    assert first.quantization is None  # no overlay records it: never guessed
    assert first.settings_source == "routing:local-a, serving:vllm unit"
    assert (first.first_seen, first.last_seen) == (
        "2026-09-01T00:00:00Z",
        "2026-11-14T01:00:00Z",
    )
    second = by[f"Model-X@{SECOND_HOST}"]
    assert (second.quantization, second.thinking, second.context_tokens) == (
        "Q3_K Medium",
        "off",
        65536,
    )
    assert (second.price_in_usd_per_mtok, second.price_out_usd_per_mtok) == (0.0, 0.0)
    glm = by["glm-flash"]
    assert (glm.engine_kind, glm.host) == ("GLM", None)
    assert (glm.price_in_usd_per_mtok, glm.price_out_usd_per_mtok) == (0.5, 2.0)
    assert glm.thinking == "unrecorded"


def test_golden_series_counts_wilson_percentiles_days_and_all_kinds() -> None:
    doc = _build()
    local = f"Model-X@{LOCAL_HOST}"
    document = _series(doc, local, "document")
    first_hour = document.hours[0]
    assert (
        first_hour.bucket,
        first_hour.attempts,
        first_hour.accepted,
        first_hour.rejected,
        first_hour.timeouts,
    ) == (
        "2026-11-13T10:00:00Z",
        10,
        8,
        1,
        1,
    )
    assert first_hour.success == ModelSuccess(
        rate=0.8, wilson_low=0.4902, wilson_high=0.9433
    )
    assert (
        first_hour.latency_runs,
        first_hour.p50_latency_ms,
        first_hour.p95_latency_ms,
    ) == (10, 500.0, 1000.0)
    assert first_hour.cost_usd == 0.5
    assert [d.bucket for d in document.days] == [
        "2026-11-13T00:00:00Z",
        "2026-11-14T00:00:00Z",
    ]
    assert document.days[1].attempts == 4
    assert document.days[1].p50_latency_ms == 50.0
    everything = _series(doc, local, "*")
    assert [d.attempts for d in everything.days] == [
        20,
        4,
    ]  # document 10 + review 10 on the 13th
    assert everything.total is not None
    assert (everything.total.attempts, everything.total.accepted) == (24, 17)
    assert (
        everything.total.bucket == "2026-10-31T02:00:00Z"
    )  # now minus 14 days, to the hour
    glm = _series(doc, "glm-flash", "document")
    assert (glm.hours[0].attempts, glm.hours[0].accepted) == (
        5,
        1,
    )  # two client hosts merge into one model


def test_golden_experiment_arms_wilson_baseline_paired_and_settings() -> None:
    exp = _build().experiments[0]
    assert (exp.id, exp.n, exp.verdict) == (
        "exp-demo",
        156,
        "Result: few-shot lifts it",
    )
    assert exp.task_set.hash == "a" * 64
    assert exp.majority_baseline is not None
    assert (exp.majority_baseline.accuracy, exp.majority_baseline.correct) == (
        0.1923,
        30,
    )
    zero, few, ghost, plain = exp.arms
    assert (zero.accuracy, zero.wilson_low, zero.wilson_high) == (
        0.7308,
        0.6562,
        0.7942,
    )
    assert zero.model == f"Model-X@{LOCAL_HOST}"
    assert (
        zero.settings == "context 131072 tokens; quantization unrecorded; thinking on"
    )
    assert few.model == f"Model-X@{SECOND_HOST}"
    assert (
        few.settings
        == "context 65536 tokens; quantization Q3_K Medium; thinking off; price 0/0 USD per Mtok"
    )
    assert (zero.p50_latency_s, zero.gpu_seconds) == (0.58, 89.8)
    assert (ghost.model, ghost.settings) == (None, None)
    assert (plain.model, plain.settings) == (None, None)
    assert [(p.test, p.p_value, p.only_a_right) for p in exp.paired] == [
        ("exact McNemar", 0.0052, 7)
    ]


def test_golden_output_is_the_same_in_any_row_order() -> None:
    req = request()
    flipped = request(
        attempts=tuple(reversed(req.attempts)),
        latencies=tuple(reversed(req.latencies)),
        seen=tuple(reversed(req.seen)),
    )
    handler = HandlerFloorDelegationHistory()
    assert (
        handler.handle(req).model_dump_json()
        == handler.handle(flipped).model_dump_json()
    )


def test_error_a_run_that_never_reached_a_model_is_no_models_attempt() -> None:
    doc = _build()
    assert all("not routed" not in m.id for m in doc.models)
    assert all("not routed" not in s.model for s in doc.series)


def test_error_an_unrecorded_setting_is_never_guessed() -> None:
    doc = _build(policy=POLICY.model_copy(update={"serving": ()}), backends=())
    for model in doc.models:
        assert model.context_tokens is None
        assert model.quantization is None
        assert model.thinking == "unrecorded"
        assert model.settings_source == "unrecorded"


def test_error_an_arm_rule_naming_a_missing_backend_is_named_in_the_sources() -> None:
    doc = _build()
    problems = [s for s in doc.sources if not s.ok]
    assert [s.id for s in problems] == ["arm_rule:^ghost"]
    assert "no-such-backend" in problems[0].detail


def test_error_a_model_with_attempts_but_no_seen_row_takes_its_times_from_its_buckets() -> (
    None
):
    doc = _build(
        seen=(
            seen(
                "local-a",
                "Model-X",
                LOCAL_HOST,
                "2026-09-01T00:00:00Z",
                "2026-11-14T01:00:00Z",
            ),
        )
    )
    glm = next(m for m in doc.models if m.id == "glm-flash")
    assert (glm.first_seen, glm.last_seen) == (
        "2026-11-14T01:00:00Z",
        "2026-11-14T01:00:00Z",
    )


def test_error_outcomes_that_do_not_add_up_are_refused_by_the_model() -> None:
    with pytest.raises(ValidationError, match="do not add up"):
        ModelSeriesPoint(
            bucket="2026-11-14T01:00:00Z",
            attempts=3,
            accepted=2,
            rejected=2,
            timeouts=0,
            latency_runs=0,
            p50_latency_ms=None,
            p95_latency_ms=None,
            cost_usd=0.0,
            success=ModelSuccess(rate=None, wilson_low=None, wilson_high=None),
        )
    with pytest.raises(
        ValidationError, match="greater_than_equal"
    ):  # accepted + timeouts above attempts: a negative rejected count is refused
        _build(
            attempts=(attempts("2026-11-14T01:00:00Z", n=3, accepted=2, timeouts=2),)
        )


def test_control_a_broken_wilson_formula_fails_the_golden_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The named injected break: the normal approximation in place of Wilson turns the interval, so the golden test would fail."""

    def normal_approximation(correct: int, n: int) -> tuple[float | None, float | None]:
        if n <= 0:
            return None, None
        p = correct / n
        half = 1.96 * (p * (1 - p) / n) ** 0.5
        return round(max(0.0, p - half), 4), round(min(1.0, p + half), 4)

    monkeypatch.setattr(handler_module, "wilson", normal_approximation)
    doc = _build()
    hour = _series(doc, f"Model-X@{LOCAL_HOST}", "document").hours[0]
    assert (hour.success.wilson_low, hour.success.wilson_high) != (0.4902, 0.9433)
