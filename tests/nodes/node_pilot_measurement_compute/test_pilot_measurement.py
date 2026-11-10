# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 16's measurement, rule by rule (OMN-23611): each rule reads the exact verdict, reasons and lists."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_pilot_measurement import (
    ModelPilotDeclaration,
    ModelPilotFact,
    ModelPilotMeasurementResult,
)
from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest_check_overlay import (
    ModelPilotManifestCheckOverlay,
)
from omnimarket.nodes.node_pilot_measurement_compute.handlers.handler_pilot_measurement import (
    HandlerPilotMeasurement,
    token_safe,
)
from tests.nodes.node_pilot_measurement_compute.pilot_support import (
    FROZEN,
    NOW,
    STARTED,
    declaration,
    fact,
    finished,
    good,
    request,
)


def read(**edits: Any) -> ModelPilotMeasurementResult:
    return HandlerPilotMeasurement().handle(request(**edits))


def test_all_bars_met_reads_working_and_the_pilot_is_accepted() -> None:
    result = read()
    assert (result.verdict, result.reasons) == (V.WORKING, ())
    assert result.accepted == ("memory",)
    assert (result.pilots_declared, result.pilots_started, result.pilots_resulted) == (
        1,
        1,
        1,
    )


def test_unread_facts_read_unknown_with_a_token_safe_reason() -> None:
    result = read(
        facts=[], facts_unread_reason="ledger-read-failed: timeout after 120 s"
    )
    assert result.verdict is V.UNKNOWN
    assert result.reasons == ("facts-unread:ledger-read-failed:-timeout-after-120-s",)
    assert (result.pilots_declared, result.pilots_started) == (1, 0)


def test_no_declared_pilot_reads_unknown() -> None:
    result = read(declarations=[], facts=[])
    assert (result.verdict, result.reasons) == (V.UNKNOWN, ("no-pilot-declared",))


def test_a_pilot_that_has_not_started_reads_unknown_and_fails_nothing() -> None:
    result = read(facts=[])
    assert (result.verdict, result.reasons) == (V.UNKNOWN, ("no-pilot-started",))
    assert (result.pilots_declared, result.pilots_started) == (1, 0)


def test_a_started_pilot_with_no_bar_hash_is_refused() -> None:
    result = read(facts=[fact(bar_hash=None)])
    assert result.verdict is V.NOT_WORKING
    assert result.reasons == ("pilot-refused:memory:hash_missing",)


def test_a_started_pilot_whose_bar_hash_is_not_the_declared_bar_is_refused() -> None:
    result = read(facts=[fact(bar_hash="f" * 64)])
    assert result.verdict is V.NOT_WORKING
    assert result.reasons == ("pilot-refused:memory:hash_mismatch",)


def test_a_pilot_that_froze_its_bar_after_it_started_is_refused() -> None:
    result = read(facts=[fact(frozen_at="2026-11-10T16:30:00Z")])
    assert result.reasons == ("pilot-refused:memory:not_frozen_before_start",)


def test_a_pilot_with_no_frozen_at_is_refused() -> None:
    assert read(facts=[fact(frozen_at=None)]).reasons == (
        "pilot-refused:memory:not_frozen_before_start",
    )


def test_a_started_pilot_with_no_result_past_its_deadline_ended_with_no_result() -> (
    None
):
    result = read(declarations=[declaration(deadline=3600)], facts=[fact()])
    assert result.verdict is V.NOT_WORKING
    assert result.reasons == ("pilot-no-result:memory",)


def test_a_started_pilot_with_no_result_inside_its_deadline_is_pending() -> None:
    result = read(declarations=[declaration(deadline=21600)], facts=[fact()])
    assert (result.verdict, result.reasons) == (V.WORKING, ())
    assert result.pending == ("memory",)


def test_a_missed_gating_bar_reads_not_working() -> None:
    result = read(facts=[finished(planted_score=3.0)])
    assert result.verdict is V.NOT_WORKING
    assert result.reasons == ("pilot-bar-missed:memory:planted_score",)
    assert result.rejected == ()


def test_a_gating_metric_missing_from_the_result_reads_not_working() -> None:
    results = {"accuracy_gain": 0.2, "ingest_hours": 1.0}
    result = read(facts=[fact(finished_at="2026-11-10T17:30:00Z", results=results)])
    assert result.reasons == ("pilot-bar-missed:memory:planted_score",)


def test_a_missed_bar_that_does_not_gate_rejects_the_pilot_and_reads_working() -> None:
    result = read(facts=[finished(accuracy_gain=0.01)])
    assert (result.verdict, result.reasons) == (V.WORKING, ())
    assert result.rejected == ("memory",)
    assert result.accepted == ()


def test_a_value_equal_to_the_threshold_meets_the_bar() -> None:
    result = read(
        facts=[finished(planted_score=5.0, accuracy_gain=0.10, ingest_hours=6.0)]
    )
    assert (result.verdict, result.accepted) == (V.WORKING, ("memory",))


def test_one_bad_pilot_among_two_names_only_that_pilot() -> None:
    result = read(
        declarations=[declaration("a"), declaration("b")],
        facts=[finished("a"), finished("b", planted_score=1.0)],
    )
    assert result.reasons == ("pilot-bar-missed:b:planted_score",)
    assert result.accepted == ("a",)
    assert result.pilots_resulted == 2


def test_a_fact_for_an_undeclared_pilot_is_ignored() -> None:
    result = read(facts=[finished("stranger", planted_score=0.0)])
    assert (result.verdict, result.reasons) == (V.UNKNOWN, ("no-pilot-started",))


def test_of_two_facts_for_one_pilot_the_one_that_started_last_decides() -> None:
    older = fact(
        bar_hash=None,
        started_at="2026-11-10T10:00:00Z",
        frozen_at="2026-11-10T09:00:00Z",
    )
    result = read(facts=[older, finished()])
    assert (result.verdict, result.reasons) == (V.WORKING, ())


def test_the_check_overlay_is_the_overlays_and_a_missing_metric_can_be_allowed() -> (
    None
):
    results = {"accuracy_gain": 0.2, "ingest_hours": 1.0}
    lenient = ModelPilotManifestCheckOverlay(missing_metric_fails_bar=False)
    outcome = HandlerPilotMeasurement(lenient).handle(
        request(facts=[fact(finished_at="2026-11-10T17:30:00Z", results=results)])
    )
    assert outcome.reasons == ()


def test_the_same_request_reads_the_same_result() -> None:
    assert read() == read()


def test_a_declaration_that_gates_on_a_metric_outside_its_bar_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelPilotDeclaration.model_validate(declaration(gating=("recall",)))


def test_a_fact_with_a_finish_stamp_and_no_results_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelPilotFact.model_validate(fact(finished_at="2026-11-10T17:30:00Z"))


def test_token_safe_cuts_and_lowercases() -> None:
    assert token_safe("A B|C") == "a-b-c"
    assert len(token_safe("x" * 200)) == 80


def test_the_good_request_is_the_one_the_other_tests_edit() -> None:
    assert good()["now"] == NOW
    assert STARTED < NOW
    assert FROZEN < STARTED
