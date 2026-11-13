# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the run outcome derivation."""

from __future__ import annotations

from omnimarket.models.model_host_hygiene_orchestrator import (
    EnumHostHygieneRunOutcome,
    EnumHostHygieneUnitOutcome,
    ModelHostHygieneUnitResult,
    run_outcome,
)


def _unit(outcome: EnumHostHygieneUnitOutcome) -> ModelHostHygieneUnitResult:
    return ModelHostHygieneUnitResult(
        host="h1",
        cleanup_id="c1",
        work_unit_id="w1",
        outcome=outcome,
    )


def test_no_units_is_nothing_due() -> None:
    assert run_outcome(()) is EnumHostHygieneRunOutcome.NOTHING_DUE


def test_all_completed_is_completed() -> None:
    units = (
        _unit(EnumHostHygieneUnitOutcome.COMPLETED),
        _unit(EnumHostHygieneUnitOutcome.COMPLETED),
    )
    assert run_outcome(units) is EnumHostHygieneRunOutcome.COMPLETED


def test_none_completed_is_failed() -> None:
    units = (
        _unit(EnumHostHygieneUnitOutcome.FAILED),
        _unit(EnumHostHygieneUnitOutcome.TIMED_OUT),
    )
    assert run_outcome(units) is EnumHostHygieneRunOutcome.FAILED


def test_mixed_is_partial() -> None:
    units = (
        _unit(EnumHostHygieneUnitOutcome.COMPLETED),
        _unit(EnumHostHygieneUnitOutcome.FAILED),
    )
    assert run_outcome(units) is EnumHostHygieneRunOutcome.PARTIAL
