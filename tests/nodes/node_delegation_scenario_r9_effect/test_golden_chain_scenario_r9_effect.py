# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scenario run of R9, malformed and injected tasks are refused (OMN-24554): golden and error chains, failing controls.

The handler under test is the real ``HandlerScenarioR9`` with the contract's runtime block; its corpus request and its
control lane are fakes. The golden chain answers every committed case with the refusal its inputs declare, so the run is
green, writes exactly one STATUS row on the R9 lane and emits one result on the bus. Every error chain breaks exactly
one thing — one case's terminal, the control lane, the inputs file — and shows the run red naming what broke. The
failing controls break every case at once.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import JsonValue

from omnimarket.handlers.delegation_scenario_runtime import InMemoryScenarioEventSink
from omnimarket.models.model_delegation_scenario import EnumScenarioConclusion
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_lane import (
    LaneReadError,
    ModelTotals,
)
from omnimarket.nodes.node_delegation_scenario_r9_effect.handlers.adapter_r9_corpus import (
    CorpusRequest,
    ModelCorpusAnswer,
)
from omnimarket.nodes.node_delegation_scenario_r9_effect.handlers.handler_scenario_r9 import (
    NODE_DIR,
    HandlerScenarioR9,
    ModelR9Case,
    ModelScenarioR9RunCommand,
    load_inputs,
    load_runtime,
)

pytestmark = pytest.mark.unit

TERMINAL = "onex.evt.omnimarket.delegation-scenario-result.v1"
NOW = datetime(2026, 11, 15, 6, 5, 30, tzinfo=UTC)
RUNTIME = load_runtime()
INPUTS = load_inputs(NODE_DIR / "inputs/r9_run.yaml")
_CASE_BY_ID = {case.id: case for case in INPUTS.cases}


def _zero_totals() -> ModelTotals:
    """The control lane's answer when the bus is alive and holds nothing."""
    return ModelTotals(
        runs=0, tokens=0, spend_usd=Decimal("0"), savings_usd=Decimal("0")
    )


def _case_of(payload: Mapping[str, JsonValue]) -> ModelR9Case:
    """The committed case whose payload is exactly the one that was published."""
    for case in INPUTS.cases:
        if case.payload == payload:
            return case
    raise AssertionError(f"an unknown corpus payload was published: {payload}")


def _refusal(
    case: ModelR9Case, changes: Mapping[str, JsonValue] | None = None
) -> ModelCorpusAnswer:
    """The refusal terminal the runtime owes this case, with ``changes`` overlaid on its payload."""
    payload: dict[str, JsonValue] = {
        "failure_code": INPUTS.refusal_failure_code,
        "retryable": INPUTS.retryable,
        "failure_reason": case.names_field or "redacted",
    }
    payload.update(changes or {})
    return ModelCorpusAnswer(answered=True, failed=True, payload=payload, reason=None)


def _success_terminal() -> ModelCorpusAnswer:
    """A terminal that completed the task instead of refusing it."""
    return ModelCorpusAnswer(
        answered=True, failed=False, payload={"status": "completed"}, reason=None
    )


def _request_with(
    by_id: Mapping[str, ModelCorpusAnswer] | None = None,
) -> CorpusRequest:
    """Every case answers the refusal its inputs declare, except the ids named in ``by_id``."""

    def request(
        payload: Mapping[str, JsonValue], timeout_s: float
    ) -> ModelCorpusAnswer:
        del timeout_s
        case = _case_of(payload)
        if by_id is not None and case.id in by_id:
            return by_id[case.id]
        return _refusal(case)

    return request


def _success_request() -> CorpusRequest:
    """A runtime that answers every corpus payload with a success terminal."""

    def request(
        payload: Mapping[str, JsonValue], timeout_s: float
    ) -> ModelCorpusAnswer:
        del payload, timeout_s
        return _success_terminal()

    return request


def _handler(
    tmp_path: Path,
    request: CorpusRequest,
    *,
    control: Callable[[], ModelTotals] = _zero_totals,
    inputs_path: Path | None = None,
) -> tuple[HandlerScenarioR9, list[str], InMemoryScenarioEventSink]:
    rows: list[str] = []
    sink = InMemoryScenarioEventSink()
    handler = HandlerScenarioR9(
        RUNTIME,
        home=tmp_path,
        environment={},
        appender=rows.append,
        sink=sink,
        terminal_topic=TERMINAL,
        request=request,
        control=control,
        clock=lambda: NOW,
        inputs_path=inputs_path,
    )
    return handler, rows, sink


def _failed(assertions: tuple[str, ...]) -> list[str]:
    return [item for item in assertions if item.startswith("-")]


# -- golden chain ---------------------------------------------------------------------------------------------------


def test_golden_chain_every_case_refused_one_status_row_one_event(
    tmp_path: Path,
) -> None:
    published: list[dict[str, JsonValue]] = []
    timeouts: list[float] = []

    def request(
        payload: Mapping[str, JsonValue], timeout_s: float
    ) -> ModelCorpusAnswer:
        published.append(dict(payload))
        timeouts.append(timeout_s)
        return _refusal(_case_of(payload))

    handler, rows, sink = _handler(tmp_path, request)

    result = handler.handle(ModelScenarioR9RunCommand())

    assert _failed(result.assertions) == [], result.detail
    assert result.conclusion is EnumScenarioConclusion.SUCCESS
    assert published == [case.payload for case in INPUTS.cases]
    assert timeouts == [INPUTS.wait_timeout_seconds] * len(INPUTS.cases)
    for case in INPUTS.cases:
        assert f"+case_{case.id}_refused" in result.assertions
        assert f"+case_{case.id}_not_retryable" in result.assertions
        if case.names_field is not None:
            assert f"+case_{case.id}_names_field" in result.assertions
    assert len(rows) == 1
    assert "lane=node_delegation_scenario_r9" in rows[0]
    assert [(topic, payload["scenario_id"]) for topic, payload in sink.events] == [
        (TERMINAL, "R9")
    ]


# -- error chains ---------------------------------------------------------------------------------------------------


def test_error_chain_a_success_terminal_is_not_a_refusal(tmp_path: Path) -> None:
    handler, _, _ = _handler(tmp_path, _request_with({"E1": _success_terminal()}))
    result = handler.handle(ModelScenarioR9RunCommand())
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert "-case_E1_refused" in result.assertions
    assert "case_E1_refused" in result.detail


def test_error_chain_a_different_failure_code_is_refused(tmp_path: Path) -> None:
    other_code = _refusal(_CASE_BY_ID["E2"], {"failure_code": "ONEX_CORE_099_OTHER"})
    handler, _, _ = _handler(tmp_path, _request_with({"E2": other_code}))
    result = handler.handle(ModelScenarioR9RunCommand())
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert "-case_E2_refused" in result.assertions
    assert "case_E2_refused" in result.detail


def test_error_chain_a_flipped_retryable_flag_is_refused(tmp_path: Path) -> None:
    flipped = _refusal(_CASE_BY_ID["E3"], {"retryable": not INPUTS.retryable})
    handler, _, _ = _handler(tmp_path, _request_with({"E3": flipped}))
    result = handler.handle(ModelScenarioR9RunCommand())
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert "-case_E3_not_retryable" in result.assertions
    assert "case_E3_not_retryable" in result.detail


def test_error_chain_a_reason_that_names_no_field_is_refused(tmp_path: Path) -> None:
    redacted = _refusal(_CASE_BY_ID["E5"], {"failure_reason": "redacted"})
    handler, _, _ = _handler(tmp_path, _request_with({"E5": redacted}))
    result = handler.handle(ModelScenarioR9RunCommand())
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert "-case_E5_names_field" in result.assertions
    assert "case_E5_names_field" in result.detail


def test_error_chain_a_case_that_was_never_answered_is_refused(tmp_path: Path) -> None:
    unanswered = ModelCorpusAnswer(
        answered=False, failed=False, payload={}, reason="no terminal: timed out"
    )
    handler, _, _ = _handler(tmp_path, _request_with({"E6": unanswered}))
    result = handler.handle(ModelScenarioR9RunCommand())
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert "-case_E6_refused" in result.assertions
    assert "case_E6_refused" in result.detail


def test_error_chain_a_control_that_does_not_read_fails_the_run_before_any_case(
    tmp_path: Path,
) -> None:
    """The control runs first: every case answers its committed refusal, and the run is still red."""

    def broken() -> ModelTotals:
        raise LaneReadError("the tenant read timed out")

    handler, _, _ = _handler(tmp_path, _request_with(), control=broken)
    result = handler.handle(ModelScenarioR9RunCommand())
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert result.assertions == ("+inputs_readable", "-control_read_completed")
    assert "the control lane did not read" in result.detail


def test_error_chain_unreadable_inputs_fail_the_run_and_still_write_the_row(
    tmp_path: Path,
) -> None:
    handler, rows, sink = _handler(
        tmp_path, _request_with(), inputs_path=tmp_path / "missing.yaml"
    )
    result = handler.handle(ModelScenarioR9RunCommand())
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert result.assertions == ("-inputs_readable",)
    assert result.detail.startswith("inputs unreadable")
    assert len(rows) == 1
    assert len(sink.events) == 1


# -- failing controls -----------------------------------------------------------------------------------------------


def test_failing_control_a_changed_refusal_code_fails_every_case(
    tmp_path: Path,
) -> None:
    """The break: the declared refusal code no longer names the code the runtime answers with."""
    raw = yaml.safe_load((NODE_DIR / "inputs/r9_run.yaml").read_text(encoding="utf-8"))
    raw["refusal_failure_code"] = "ONEX_CORE_099_OTHER"
    changed = tmp_path / "changed-refusal-code.yaml"
    changed.write_text(yaml.safe_dump(raw), encoding="utf-8")

    handler, _, _ = _handler(tmp_path, _request_with(), inputs_path=changed)
    result = handler.handle(ModelScenarioR9RunCommand())

    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert "+control_read_completed" in result.assertions
    for case in INPUTS.cases:
        assert f"-case_{case.id}_refused" in result.assertions


def test_failing_control_a_request_that_answers_success_fails_every_case(
    tmp_path: Path,
) -> None:
    handler, _, _ = _handler(tmp_path, _success_request())
    result = handler.handle(ModelScenarioR9RunCommand())
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert "+control_read_completed" in result.assertions
    for case in INPUTS.cases:
        assert f"-case_{case.id}_refused" in result.assertions
