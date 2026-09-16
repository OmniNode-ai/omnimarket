# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the outage circuit-breaker control loop (OMN-14774 / F-07).

The merge-check classifier already EMITS ``GITHUB_API_OUTAGE`` (OMN-14765); this
module is the CONSUMER — the active pause / circuit-breaker control loop that
withholds REST-dependent mutations during a detected outage and gates resumption
on a recovery probe. These tests prove the state machine directly (no network,
no orchestrator), covering the three ticket acceptance criteria at the unit
level plus the fail-closed bounds.

OMN-18429 gave the breaker a declared trip threshold. The state-machine cases
below are about the machine and not the threshold, so they run under an explicit
floor-of-one policy, which is exactly the OMN-14774 semantics stated as a policy
rather than assumed. The threshold itself is pinned separately at the bottom of
this module and end to end in
``tests/test_pr_lifecycle_outage_threshold_omn18429.py``.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from omnimarket.merge_control.model_outage_breaker_policy import (
    ModelOutageBreakerPolicy,
)
from omnimarket.merge_control.outage_circuit_breaker import (
    EnumOutageBreakerState,
    OutageCircuitBreaker,
)
from omnimarket.merge_control.reason_code_classifier import EnumMergeCheckReasonCode

_OUTAGE = str(EnumMergeCheckReasonCode.GITHUB_API_OUTAGE)

# The OMN-14774 semantics, stated: any single affected pull request trips.
_TRIPS_ON_ONE = ModelOutageBreakerPolicy(
    min_outage_prs=1, min_outage_fraction=0.0, min_window_observations=1
)


def _breaker(
    policy: ModelOutageBreakerPolicy = _TRIPS_ON_ONE, **kwargs: object
) -> OutageCircuitBreaker:
    return OutageCircuitBreaker(policy=policy, **kwargs)  # type: ignore[arg-type]


def _one_pr(codes: Iterable[object]) -> list[tuple[tuple[str, int], list[str]]]:
    """One pull request carrying ``codes`` — the old flat-list case, keyed."""
    return [(("OmniNode-ai/omnimarket", 1), [str(code) for code in codes])]


def _prs(outage: int, clean: int) -> list[tuple[tuple[str, int], list[str]]]:
    """A window of ``outage + clean`` pull requests, ``outage`` of them affected."""
    rows: list[tuple[tuple[str, int], list[str]]] = [
        (("OmniNode-ai/omnimarket", n), [_OUTAGE]) for n in range(outage)
    ]
    rows += [
        (("OmniNode-ai/omnimarket", 1000 + n), ["product_failure"])
        for n in range(clean)
    ]
    return rows


@pytest.mark.unit
class TestObserveOpensBreaker:
    """Acceptance 1 (unit): a GITHUB_API_OUTAGE reason code trips the breaker."""

    def test_starts_closed_mutations_allowed(self) -> None:
        breaker = _breaker()
        assert breaker.state is EnumOutageBreakerState.CLOSED
        assert breaker.mutations_allowed is True
        assert breaker.is_open is False

    def test_outage_code_opens_breaker_and_withholds_mutations(self) -> None:
        breaker = _breaker()
        state = breaker.observe_pass(_one_pr([_OUTAGE]))
        assert state is EnumOutageBreakerState.OPEN
        assert breaker.is_open is True
        assert breaker.mutations_allowed is False
        assert breaker.open_count == 1

    def test_outage_code_as_enum_opens_breaker(self) -> None:
        breaker = _breaker()
        breaker.observe_pass(_one_pr([EnumMergeCheckReasonCode.GITHUB_API_OUTAGE]))
        assert breaker.is_open is True

    def test_outage_mixed_with_other_codes_still_opens(self) -> None:
        breaker = _breaker()
        breaker.observe_pass(
            _one_pr(
                [
                    EnumMergeCheckReasonCode.PRODUCT_FAILED,
                    EnumMergeCheckReasonCode.RUNNER_INFRA,
                    _OUTAGE,
                ]
            )
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
        breaker = _breaker()
        state = breaker.observe_pass(_one_pr(codes))
        assert state is EnumOutageBreakerState.CLOSED
        assert breaker.mutations_allowed is True
        assert breaker.open_count == 0

    def test_repeated_outage_observation_does_not_double_open(self) -> None:
        breaker = _breaker()
        breaker.observe_pass(_one_pr([_OUTAGE]))
        breaker.observe_pass(_one_pr([_OUTAGE]))
        # Still exactly one CLOSED->OPEN transition.
        assert breaker.open_count == 1
        assert breaker.is_open is True

    def test_last_observed_outage_flag_tracks_input(self) -> None:
        breaker = _breaker()
        breaker.observe_pass(_one_pr([str(EnumMergeCheckReasonCode.RUNNER_INFRA)]))
        assert breaker.last_observed_outage is False
        breaker.observe_pass(_one_pr([_OUTAGE]))
        assert breaker.last_observed_outage is True


@pytest.mark.unit
class TestRecoveryProbeGate:
    """Acceptance 2 (unit): resumption is gated on a recovery-probe pass."""

    def test_probe_fail_keeps_breaker_open(self) -> None:
        breaker = _breaker()
        breaker.observe_pass(_one_pr([_OUTAGE]))
        resumed = breaker.probe_recovery(lambda: False)
        assert resumed is False
        assert breaker.is_open is True
        assert breaker.mutations_allowed is False
        assert breaker.consecutive_probe_failures == 1

    def test_probe_pass_closes_breaker_and_resumes(self) -> None:
        breaker = _breaker()
        breaker.observe_pass(_one_pr([_OUTAGE]))
        resumed = breaker.probe_recovery(lambda: True)
        assert resumed is True
        assert breaker.state is EnumOutageBreakerState.CLOSED
        assert breaker.mutations_allowed is True
        assert breaker.consecutive_probe_failures == 0

    def test_probe_fail_then_pass_resumes(self) -> None:
        breaker = _breaker()
        breaker.observe_pass(_one_pr([_OUTAGE]))
        assert breaker.probe_recovery(lambda: False) is False
        assert breaker.is_open is True
        assert breaker.probe_recovery(lambda: True) is True
        assert breaker.mutations_allowed is True

    def test_raising_probe_counts_as_failed_probe_fail_closed(self) -> None:
        def _boom() -> bool:
            msg = "api.github.com unreachable"
            raise RuntimeError(msg)

        breaker = _breaker()
        breaker.observe_pass(_one_pr([_OUTAGE]))
        resumed = breaker.probe_recovery(_boom)
        assert resumed is False
        assert breaker.is_open is True
        assert breaker.consecutive_probe_failures == 1

    def test_probe_on_closed_breaker_is_noop_true(self) -> None:
        breaker = _breaker()
        # never opened
        assert breaker.probe_recovery(lambda: False) is True
        assert breaker.mutations_allowed is True

    def test_probe_budget_bounds_reprobing_fail_closed(self) -> None:
        breaker = _breaker(max_probe_attempts=2)
        breaker.observe_pass(_one_pr([_OUTAGE]))
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
            _breaker(max_probe_attempts=0)


@pytest.mark.unit
class TestWithheldBookkeeping:
    def test_record_withheld_accumulates(self) -> None:
        breaker = _breaker()
        breaker.observe_pass(_one_pr([_OUTAGE]))
        breaker.record_withheld(3)
        breaker.record_withheld(2)
        assert breaker.mutations_withheld == 5

    def test_record_withheld_ignores_nonpositive(self) -> None:
        breaker = _breaker()
        breaker.record_withheld(0)
        breaker.record_withheld(-4)
        assert breaker.mutations_withheld == 0


@pytest.mark.unit
class TestDeclaredThresholdDecidesThePass:
    """OMN-18429: one unreliable fetch is evidence about one pull request."""

    def test_one_affected_pull_request_in_a_wide_window_does_not_trip(self) -> None:
        breaker = _breaker(
            ModelOutageBreakerPolicy(
                min_outage_prs=3, min_outage_fraction=0.25, min_window_observations=8
            )
        )
        state = breaker.observe_pass(_prs(outage=1, clean=55))
        assert state is EnumOutageBreakerState.CLOSED
        assert breaker.mutations_allowed is True
        # The per-pull-request verdict still stands, unconditionally.
        assert len(breaker.unknown_pr_keys) == 1
        assert breaker.observed_pr_count == 56

    def test_a_wide_outage_trips(self) -> None:
        """Positive control: without this the case above proves only a dead breaker."""
        breaker = _breaker(
            ModelOutageBreakerPolicy(
                min_outage_prs=3, min_outage_fraction=0.25, min_window_observations=8
            )
        )
        assert (
            breaker.observe_pass(_prs(outage=40, clean=16))
            is EnumOutageBreakerState.OPEN
        )

    def test_the_floor_and_the_fraction_are_both_required(self) -> None:
        policy = ModelOutageBreakerPolicy(
            min_outage_prs=3, min_outage_fraction=0.25, min_window_observations=8
        )
        # Clears the fraction (2/8 = 0.25) but not the floor.
        assert not policy.trips(outage_pr_count=2, observed_pr_count=8)
        # Clears the floor but not the fraction (3/56 = 0.05).
        assert not policy.trips(outage_pr_count=3, observed_pr_count=56)
        # Clears both.
        assert policy.trips(outage_pr_count=3, observed_pr_count=12)

    def test_a_window_below_the_minimum_falls_back_to_the_floor(self) -> None:
        policy = ModelOutageBreakerPolicy(
            min_outage_prs=3, min_outage_fraction=0.25, min_window_observations=8
        )
        # 3 of 3 is a fraction of 1.0, but the window is too narrow to measure;
        # the floor cleared, so it trips — the conservative direction.
        assert policy.trips(outage_pr_count=3, observed_pr_count=3)
        assert not policy.trips(outage_pr_count=2, observed_pr_count=3)

    def test_evidence_below_the_threshold_never_closes_an_open_breaker(self) -> None:
        """Only a passing recovery probe resumes. Fail-closed is unchanged."""
        breaker = _breaker(
            ModelOutageBreakerPolicy(
                min_outage_prs=1, min_outage_fraction=0.0, min_window_observations=1
            )
        )
        breaker.observe_pass(_prs(outage=1, clean=0))
        assert breaker.is_open is True
        breaker.observe_pass(_prs(outage=0, clean=10))
        assert breaker.is_open is True, (
            "A clean re-observation inside one pass must not resume mutations; "
            "the cross-pass recovery path is a fresh breaker on the next sweep."
        )

    def test_the_policy_has_no_default(self) -> None:
        """No undeclared threshold anywhere, and no environment variable."""
        with pytest.raises(TypeError):
            OutageCircuitBreaker()  # type: ignore[call-arg]
