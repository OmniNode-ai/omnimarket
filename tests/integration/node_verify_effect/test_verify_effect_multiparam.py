# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_verify_effect (WS-5 Wave 4).

Variant A (EFFECT direct in-process handler call). Drives the real HandlerVerify
across distinct checker compositions (all-pass, critical-fail, non-critical-fail,
raising-checker, dry-run) and asserts the TYPED ModelVerifyResult fields
(all_critical_passed, per-check pass/critical, warnings).

Health checkers are injected via the constructor (ProtocolHealthChecker) — no
dashboard/runtime/data-flow network I/O.

Negative control: a known-bad fixture (a failing CRITICAL checker) MUST flip
all_critical_passed to False — the verify effect cannot report green when a
critical dependency is down.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_verify_effect.handlers.handler_verify import HandlerVerify
from omnimarket.nodes.node_verify_effect.models.model_verify_check import (
    ModelVerifyCheck,
)


class _StubChecker:
    """Returns a fixed ModelVerifyCheck (no I/O)."""

    def __init__(self, name: str, *, passed: bool, critical: bool) -> None:
        self._name = name
        self._passed = passed
        self._critical = critical

    async def check(self, correlation_id: UUID) -> ModelVerifyCheck:
        return ModelVerifyCheck(
            name=self._name,
            passed=self._passed,
            critical=self._critical,
            message="OK" if self._passed else "DOWN",
        )


class _RaisingChecker:
    """Raises to exercise the handler's exception-capture path."""

    async def check(self, correlation_id: UUID) -> ModelVerifyCheck:
        raise RuntimeError("probe connection refused")


def _all_pass() -> list[_StubChecker]:
    return [
        _StubChecker("dashboard_health", passed=True, critical=False),
        _StubChecker("runtime_health", passed=True, critical=True),
        _StubChecker("data_flow", passed=True, critical=False),
    ]


def _critical_fail() -> list[_StubChecker]:
    return [
        _StubChecker("dashboard_health", passed=True, critical=False),
        _StubChecker("runtime_health", passed=False, critical=True),
        _StubChecker("data_flow", passed=True, critical=False),
    ]


def _noncritical_fail() -> list[_StubChecker]:
    return [
        _StubChecker("dashboard_health", passed=False, critical=False),
        _StubChecker("runtime_health", passed=True, critical=True),
    ]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("checkers", "dry_run", "expect_pass", "expect_n_checks", "expect_warns"),
    [
        pytest.param(None, True, True, 3, 1, id="dry-run-vacuous-pass"),
        pytest.param(_all_pass(), False, True, 3, 0, id="all-checkers-pass"),
        pytest.param(_critical_fail(), False, False, 3, 0, id="critical-fail-blocks"),
        pytest.param(
            _noncritical_fail(), False, True, 2, 1, id="noncritical-fail-warns-only"
        ),
    ],
)
async def test_verify_effect_multiparam(
    checkers: list[_StubChecker] | None,
    dry_run: bool,
    expect_pass: bool,
    expect_n_checks: int,
    expect_warns: int,
) -> None:
    correlation_id = uuid4()
    handler = HandlerVerify(checkers=checkers)  # type: ignore[arg-type]

    result = await handler.handle(correlation_id, dry_run=dry_run)

    assert result.correlation_id == correlation_id
    assert result.all_critical_passed is expect_pass
    assert len(result.checks) == expect_n_checks
    assert len(result.warnings) == expect_warns
    # Every critical check must actually be present and evaluated.
    critical_checks = [c for c in result.checks if c.critical]
    assert critical_checks, "at least one critical check must run"
    assert all(c.passed for c in critical_checks) is expect_pass


@pytest.mark.integration
@pytest.mark.asyncio
async def test_verify_effect_negative_control_raising_checker_captured() -> None:
    """A checker that raises is captured as a non-critical failure + warning."""
    handler = HandlerVerify(
        checkers=[
            _RaisingChecker(),  # type: ignore[list-item]
            _StubChecker("runtime_health", passed=True, critical=True),
        ]
    )
    result = await handler.handle(uuid4())
    # The raising checker did not crash the phase; it surfaced as a warning.
    assert len(result.warnings) == 1
    assert "probe connection refused" in result.warnings[0]
    # Critical runtime check still passed, so the loop is not blocked.
    assert result.all_critical_passed is True
    assert any(not c.passed for c in result.checks)
