# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Assertions of a golden or error scenario against what a run observed."""

from __future__ import annotations

from collections.abc import Mapping

from omnibase_core.enums.enum_chain_scenario_kind import EnumChainScenarioKind
from omnibase_core.enums.enum_chain_scenario_reading import EnumChainScenarioReading
from omnibase_core.models.contracts.subcontracts.model_chain_scenario_case import (
    ModelChainScenarioCase,
)
from omnibase_core.models.contracts.subcontracts.model_chain_scenario_subcontract import (
    ModelChainScenarioSubcontract,
)

from omnimarket.chain_scenarios.errors import (
    ChainScenarioAssertionError,
    EnumChainAssertionCode,
)
from omnimarket.chain_scenarios.model_chain_scenario_run import ModelChainScenarioRun

_C = EnumChainAssertionCode


def fields_match(payload: Mapping[str, object], fields: Mapping[str, object]) -> bool:
    """Every stated field is present in the payload with the stated value."""
    return all(
        key in payload and payload[key] == value for key, value in fields.items()
    )


def assert_scenario(
    run: ModelChainScenarioRun,
    case: ModelChainScenarioCase,
    scenario: ModelChainScenarioSubcontract,
) -> None:
    """Raise ``ChainScenarioAssertionError`` unless the run is what the case states."""
    golden = case.kind is EnumChainScenarioKind.GOLDEN
    if run.handler_error is not None and golden:
        raise ChainScenarioAssertionError(_C.HANDLER_ERROR, run.handler_error)
    if not run.events:
        raise ChainScenarioAssertionError(
            _C.SILENCE,
            "no event was produced"
            + (f" (handler raised: {run.handler_error})" if run.handler_error else "")
            + (" (the deadline passed)" if run.timed_out else ""),
        )
    topics = [event.topic for event in run.events]
    expected = [event.topic for event in case.expected_events]
    if topics != expected:
        raise ChainScenarioAssertionError(
            _C.EVENT_ORDER, f"produced {topics}, expected {expected}"
        )
    for seen, want in zip(run.events, case.expected_events, strict=True):
        if not fields_match(seen.payload, want.fields):
            raise ChainScenarioAssertionError(
                _C.EVENT_FIELDS,
                f"{seen.topic}: payload {dict(seen.payload)} lacks {dict(want.fields)}",
            )
    for index, seen in enumerate(run.events):
        if seen.correlation_id != run.correlation_id:
            raise ChainScenarioAssertionError(
                _C.CORRELATION,
                f"event {index} on {seen.topic} carries {seen.correlation_id}, expected {run.correlation_id}",
            )
    last = run.events[-1]
    if case.kind is EnumChainScenarioKind.GOLDEN:
        _golden(run, case, scenario)
    else:
        _error(run, case, last.failure)
    for update in case.state_updates:
        if (update.target, update.key) not in {
            (target, key)
            for _binding, target, key in run.state_writes
            if _binding == update.binding
        }:
            raise ChainScenarioAssertionError(
                _C.STATE_UPDATE,
                f"{update.binding} recorded no write of {update.key} to {update.target}",
            )


def _golden(
    run: ModelChainScenarioRun,
    case: ModelChainScenarioCase,
    scenario: ModelChainScenarioSubcontract,
) -> None:
    if run.events[-1].topic != scenario.success_topic:
        raise ChainScenarioAssertionError(
            _C.EVENT_ORDER,
            f"the chain ends on {run.events[-1].topic}, not the success event {scenario.success_topic}",
        )
    for seen in run.events:
        if seen.failure is not None:
            raise ChainScenarioAssertionError(
                _C.GOLDEN_ENDS_IN_FAILURE,
                f"{seen.topic} reports the failure {seen.failure}",
            )


def _error(
    run: ModelChainScenarioRun, case: ModelChainScenarioCase, last_failure: str | None
) -> None:
    assert case.fault is not None
    assert case.ledger_failure_row is not None
    if last_failure != case.failure:
        raise ChainScenarioAssertionError(
            _C.FAILURE_EVENT,
            f"the chain ends in failure {last_failure!r}, expected {case.failure!r}",
        )
    row = case.ledger_failure_row
    if not any(
        r.row_type == row.row_type
        and r.subject == row.subject
        and r.failure == case.failure
        and r.correlation_id == run.correlation_id
        for r in run.ledger_rows
    ):
        raise ChainScenarioAssertionError(
            _C.LEDGER_ROW,
            f"no {row.row_type} failure row for {row.subject} under {run.correlation_id}",
        )
    reading = run.readings.get(row.subject, EnumChainScenarioReading.UNKNOWN)
    if reading is not case.reading:
        raise ChainScenarioAssertionError(
            _C.READING, f"{row.subject} reads {reading.value}, expected {case.reading}"
        )
    for binding, calls in run.dependency_calls.items():
        if binding == case.fault.boundary and calls > case.fault.max_dependency_calls:
            raise ChainScenarioAssertionError(
                _C.BARE_RETRY,
                f"{binding} was called {calls} times, at most {case.fault.max_dependency_calls}",
            )


__all__ = ["assert_scenario", "fields_match"]
