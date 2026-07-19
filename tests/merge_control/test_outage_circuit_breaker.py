# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the outage circuit-breaker control loop (OMN-14774 / F-07).

The merge-check classifier already EMITS ``GITHUB_API_OUTAGE`` (OMN-14765); this
module is the CONSUMER — the active pause / circuit-breaker control loop that
withholds REST-dependent mutations during a detected outage and gates resumption
on a recovery probe. These tests prove the state machine directly (no network,
no orchestrator), covering the three ticket acceptance criteria at the unit
level plus the fail-closed bounds.
"""

from __future__ import annotations

import pytest

from omnimarket.merge_control.outage_circuit_breaker import (
    EnumOutageBreakerState,
    OutageCircuitBreaker,
)
from omnimarket.merge_control.reason_code_classifier import EnumMergeCheckReasonCode

_OUTAGE = str(EnumMergeCheckReasonCode.GITHUB_API_OUTAGE)


@pytest.mark.unit
class TestObserveOpensBreaker:
    """Acceptance 1 (unit): a GITHUB_API_OUTAGE reason code trips the breaker."""

    def test_starts_closed_mutations_allowed(self) -> None:
        breaker = OutageCircuitBreaker()
        assert breaker.state is EnumOutageBreakerState.CLOSED
        assert breaker.mutations_allowed is True
        assert breaker.is_open is False

    def test_outage_code_opens_breaker_and_withholds_mutations(self) -> None:
        breaker = OutageCircuitBreaker()
        state = breaker.observe([_OUTAGE])
        assert state is EnumOutageBreakerState.OPEN
        assert breaker.is_open is True
        assert breaker.mutations_allowed is False
        assert breaker.open_count == 1

    def test_outage_code_as_enum_opens_breaker(self) -> None:
        breaker = OutageCircuitBreaker()
        breaker.observe([EnumMergeCheckReasonCode.GITHUB_API_OUTAGE])
        assert breaker.is_open is True

    def test_outage_mixed_with_other_codes_still_opens(self) -> None:
        breaker = OutageCircuitBreaker()
        breaker.observe(
            [
                EnumMergeCheckReasonCode.PRODUCT_FAILED,
                EnumMergeCheckReasonCode.RUNNER_INFRA,
                _OUTAGE,
            ]
        )
        assert breaker.is_open is True

    @pytest.mark.parametrize(
        "codes",
        [
            (),
            (str(EnumMergeCheckReasonCode.PRODUCT_FAILED),),
            (str(EnumMergeCheckReasonCode.RUNNER_INFRA),),
            (str(EnumMergeCheckReasonCode.CANCELLED),),
            (str(EnumMergeCheckReasonCode.STALE_CONTEXT),),
        ],
    )
    def test_non_outage_codes_leave_breaker_closed(
        self, codes: tuple[str, ...]
    ) -> None:
        breaker = OutageCircuitBreaker()
        state = breaker.observe(codes)
        assert state is EnumOutageBreakerState.CLOSED
        assert breaker.mutations_allowed is True
        assert breaker.open_count == 0

    def test_repeated_outage_observation_does_not_double_open(self) -> None:
        breaker = OutageCircuitBreaker()
        breaker.observe([_OUTAGE])
        breaker.observe([_OUTAGE])
        # Still exactly one CLOSED->OPEN transition.
        assert breaker.open_count == 1
        assert breaker.is_open is True

    def test_last_observed_outage_flag_tracks_input(self) -> None:
        breaker = OutageCircuitBreaker()
        breaker.observe([str(EnumMergeCheckReasonCode.RUNNER_INFRA)])
        assert breaker.last_observed_outage is False
        breaker.observe([_OUTAGE])
        assert breaker.last_observed_outage is True


@pytest.mark.unit
class TestRecoveryProbeGate:
    """Acceptance 2 (unit): resumption is gated on a recovery-probe pass."""

    def test_probe_fail_keeps_breaker_open(self) -> None:
        breaker = OutageCircuitBreaker()
        breaker.observe([_OUTAGE])
        resumed = breaker.probe_recovery(lambda: False)
        assert resumed is False
        assert breaker.is_open is True
        assert breaker.mutations_allowed is False
        assert breaker.consecutive_probe_failures == 1

    def test_probe_pass_closes_breaker_and_resumes(self) -> None:
        breaker = OutageCircuitBreaker()
        breaker.observe([_OUTAGE])
        resumed = breaker.probe_recovery(lambda: True)
        assert resumed is True
        assert breaker.state is EnumOutageBreakerState.CLOSED
        assert breaker.mutations_allowed is True
        assert breaker.consecutive_probe_failures == 0

    def test_probe_fail_then_pass_resumes(self) -> None:
        breaker = OutageCircuitBreaker()
        breaker.observe([_OUTAGE])
        assert breaker.probe_recovery(lambda: False) is False
        assert breaker.is_open is True
        assert breaker.probe_recovery(lambda: True) is True
        assert breaker.mutations_allowed is True

    def test_raising_probe_counts_as_failed_probe_fail_closed(self) -> None:
        def _boom() -> bool:
            msg = "api.github.com unreachable"
            raise RuntimeError(msg)

        breaker = OutageCircuitBreaker()
        breaker.observe([_OUTAGE])
        resumed = breaker.probe_recovery(_boom)
        assert resumed is False
        assert breaker.is_open is True
        assert breaker.consecutive_probe_failures == 1

    def test_probe_on_closed_breaker_is_noop_true(self) -> None:
        breaker = OutageCircuitBreaker()
        # never opened
        assert breaker.probe_recovery(lambda: False) is True
        assert breaker.mutations_allowed is True

    def test_probe_budget_bounds_reprobing_fail_closed(self) -> None:
        breaker = OutageCircuitBreaker(max_probe_attempts=2)
        breaker.observe([_OUTAGE])
        assert breaker.probe_recovery(lambda: False) is False  # failure 1
        assert (
            breaker.probe_recovery(lambda: False) is False
        )  # failure 2 -> budget spent
        assert breaker.probe_budget_exhausted is True
        # A subsequent probe (even one that WOULD pass) is not run — stays OPEN.
        probe_calls: list[int] = []

        def _would_pass() -> bool:
            probe_calls.append(1)
            return True

        assert breaker.probe_recovery(_would_pass) is False
        assert probe_calls == []  # probe not invoked once budget exhausted
        assert breaker.is_open is True

    def test_invalid_max_probe_attempts_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_probe_attempts"):
            OutageCircuitBreaker(max_probe_attempts=0)


@pytest.mark.unit
class TestWithheldBookkeeping:
    def test_record_withheld_accumulates(self) -> None:
        breaker = OutageCircuitBreaker()
        breaker.observe([_OUTAGE])
        breaker.record_withheld(3)
        breaker.record_withheld(2)
        assert breaker.mutations_withheld == 5

    def test_record_withheld_ignores_nonpositive(self) -> None:
        breaker = OutageCircuitBreaker()
        breaker.record_withheld(0)
        breaker.record_withheld(-4)
        assert breaker.mutations_withheld == 0
