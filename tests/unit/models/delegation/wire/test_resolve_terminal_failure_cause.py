# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classifier tests for OMN-16998 — a terminal names the status class observed.

RED (pre-fix): the enum carried one member, so ``resolve_terminal_failure_cause``
had two outcomes — ``PROVIDER_QUOTA_EXHAUSTED`` or ``None``. A live run refused
with ``HTTP 401 Authentication Failed`` and terminalised as
``provider_quota_exhausted``, because the fallback regex matched quota wording
with no status corroboration at all. B7 ("zero over-quota refusals") is measured
from this field, so auth failures were being counted as capacity pressure.

GREEN: 401/403 resolve to ``AUTH_FAILED``; 429 carrying a recognised quota body
resolves to ``PROVIDER_QUOTA_EXHAUSTED``; any other observed provider failure
resolves to ``PROVIDER_ERROR``; and no path can reach the quota member without a
429 in the observed response.

Hermetic — no provider, no lane, no bus.
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
    resolve_terminal_failure_cause,
)


def _attempt(
    *,
    error_message: str = "",
    failure_class: str | None = None,
) -> ModelDelegateSkillAttemptRecord:
    """A failed ladder attempt carrying the given observed evidence."""
    return ModelDelegateSkillAttemptRecord(
        tier="local",
        backend_id="backend-under-test",
        model_id="model-under-test",
        quality_gate_passed=False,
        failure_class=failure_class,
        error_message=error_message,
    )


@pytest.mark.unit
class TestObservedStatusClassMapsToCause:
    """Each provider status class resolves to its own distinct cause."""

    @pytest.mark.parametrize(
        "observed",
        [
            "HTTP 401 Authentication Failed",
            "provider returned 403 Forbidden",
        ],
    )
    def test_rejected_credential_resolves_to_auth_failed(self, observed: str) -> None:
        """401 and 403 are credential rejections, never capacity signals.

        This is the OMN-16998 defect verbatim: the live terminal carried
        ``provider_quota_exhausted`` while ``quality_gates_failed[0]`` was the
        raw ``HTTP 401 Authentication Failed``.
        """
        cause = resolve_terminal_failure_cause([_attempt(error_message=observed)])
        assert cause is EnumDelegationTerminalFailureCause.AUTH_FAILED

    @pytest.mark.parametrize(
        "observed",
        [
            "HTTP 429 quota exceeded for this project",
            "429 RESOURCE_EXHAUSTED",
            "got 429: rate limit exceeded, retry later",
        ],
    )
    def test_429_with_quota_body_resolves_to_quota_exhausted(
        self, observed: str
    ) -> None:
        """A genuine capacity refusal still classifies as one."""
        cause = resolve_terminal_failure_cause([_attempt(error_message=observed)])
        assert cause is EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED

    @pytest.mark.parametrize(
        "observed",
        [
            "HTTP 500 Internal Server Error",
            "endpoint https://backend.invalid failed health probe",
            "HTTP 429 Too Many Requests",
        ],
    )
    def test_any_other_observed_failure_resolves_to_provider_error(
        self, observed: str
    ) -> None:
        """Anything else observed is a provider error, not silence.

        The bare-429 row is deliberate: the DoD scopes the quota cause to a 429
        *with a recognised quota body*, so a 429 whose body says nothing about
        quota lands here rather than inflating the quota metric.
        """
        cause = resolve_terminal_failure_cause([_attempt(error_message=observed)])
        assert cause is EnumDelegationTerminalFailureCause.PROVIDER_ERROR

    def test_outer_error_message_is_observed_evidence(self) -> None:
        """Status may arrive on the outer error rather than an attempt."""
        cause = resolve_terminal_failure_cause(
            [], error_message="HTTP 401 Authentication Failed"
        )
        assert cause is EnumDelegationTerminalFailureCause.AUTH_FAILED


@pytest.mark.unit
class TestQuotaCauseRequires429:
    """DoD invariant: no classifier path emits a quota cause without a 429."""

    @pytest.mark.parametrize(
        "observed",
        [
            "resource_exhausted",
            "quota exceeded",
            "quota_exceeded",
            "rate limit exceeded",
        ],
    )
    def test_quota_wording_without_429_is_not_a_quota_cause(
        self, observed: str
    ) -> None:
        """Quota wording alone is not a capacity fact.

        This is precisely the pre-fix fallback: the old regex matched these
        strings with no status corroboration, which is how an unclassified
        failure acquired a quota label.
        """
        cause = resolve_terminal_failure_cause([_attempt(error_message=observed)])
        assert cause is not EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED

    def test_typed_quota_claim_without_429_is_demoted(self) -> None:
        """A port's own quota claim is not exempt from the invariant.

        The DoD says *no* classifier path, which includes the typed
        ``failure_class`` path that otherwise takes precedence over the text
        fallback. An uncorroborated typed claim degrades to PROVIDER_ERROR
        rather than being trusted into the quota metric.
        """
        cause = resolve_terminal_failure_cause(
            [_attempt(failure_class="provider_quota_exhausted", error_message="")]
        )
        assert cause is EnumDelegationTerminalFailureCause.PROVIDER_ERROR

    def test_typed_quota_claim_with_429_is_honoured(self) -> None:
        """Corroborated typed evidence still resolves to the quota cause."""
        cause = resolve_terminal_failure_cause(
            [
                _attempt(
                    failure_class="provider_quota_exhausted",
                    error_message="HTTP 429 quota exceeded",
                )
            ]
        )
        assert cause is EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED


@pytest.mark.unit
class TestTypedEvidencePrecedence:
    """The documented typed-evidence-first contract survives this change."""

    def test_typed_auth_class_is_authoritative(self) -> None:
        """A port that classifies its own failure is still believed."""
        cause = resolve_terminal_failure_cause(
            [_attempt(failure_class="auth_failed", error_message="")]
        )
        assert cause is EnumDelegationTerminalFailureCause.AUTH_FAILED

    def test_first_classified_attempt_wins(self) -> None:
        """Ladder order decides between two classified attempts."""
        cause = resolve_terminal_failure_cause(
            [
                _attempt(failure_class="auth_failed"),
                _attempt(failure_class="provider_error"),
            ]
        )
        assert cause is EnumDelegationTerminalFailureCause.AUTH_FAILED

    def test_unrecognised_failure_class_falls_through_to_observed_text(self) -> None:
        """An unknown ``failure_class`` must not shadow the observed status."""
        cause = resolve_terminal_failure_cause(
            [
                _attempt(
                    failure_class="model_unavailable",
                    error_message="HTTP 401 Authentication Failed",
                )
            ]
        )
        assert cause is EnumDelegationTerminalFailureCause.AUTH_FAILED


@pytest.mark.unit
class TestNoObservedFailure:
    """Absence of evidence stays absent — it does not become PROVIDER_ERROR."""

    def test_empty_ladder_resolves_to_none(self) -> None:
        """No attempts and no outer error names no cause."""
        assert resolve_terminal_failure_cause([]) is None

    def test_attempt_without_evidence_resolves_to_none(self) -> None:
        """A failed attempt that reported nothing stays unclassified.

        ``ModelDelegateSkillResponse`` forbids a successful delegation from
        carrying a cause, so manufacturing PROVIDER_ERROR from silence would
        make a success unconstructible.
        """
        assert resolve_terminal_failure_cause([_attempt()]) is None
