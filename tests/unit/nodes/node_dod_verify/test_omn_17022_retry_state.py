# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17022 — per-item retry state for dod_verify/dod_sweep reconciliation.

Off-rails rev 2 §4.3 A15. Before this ticket, ``EnumDodVerifyStatus`` was
``PENDING | VERIFIED | FAILED | SKIPPED`` and ``ModelDodVerifyState.status``
defaulted to ``PENDING``, so a run killed by a caller-side timeout was
byte-indistinguishable from one that never started. Ten items held in the
2026-08-29 sprint-triage closeout were never re-run for exactly that reason.

These tests pin four properties the DoD names:

1. a typed terminal unresolved status exists and is persisted per item;
2. ``RUN_ERROR_OR_TIMEOUT`` is a real typed code, not a string the ad-hoc
   batch runner invented;
3. bounded backoff applies to the RUN_ERROR_OR_TIMEOUT class ONLY — a
   ``PR_LOOKUP_FAILED`` credential/resolution defect is refused a retry
   because retrying reproduces it exactly;
4. an unresolved item can never decay back to PENDING.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)
from omnimarket.events.dod_verify_retry import (
    CANONICAL_DOD_VERIFY_RETRY_POLICY,
    EnumDodVerifyRetryDisposition,
    ModelDodVerifyRetryPolicy,
    ModelDodVerifyRetryState,
    plan_next_attempt,
    reconcile_abandoned_attempt,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    ModelDodVerifyState,
)
from omnimarket.protocols.protocol_dod_verify_retry_ledger import (
    FilesystemDodVerifyRetryLedger,
)

pytestmark = pytest.mark.unit

_T0 = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)

# The exact ten items held unadjudicated by the 2026-08-29 sprint-triage
# closeout. Nine were labelled RUN_ERROR_OR_TIMEOUT by the ad-hoc batch runner;
# OMN-14993 was the one already-typed PR_LOOKUP_FAILED.
_NINE_TIMED_OUT: tuple[str, ...] = (
    "OMN-16690",
    "OMN-16785",
    "OMN-16685",
    "OMN-16838",
    "OMN-16418",
    "OMN-16293",
    "OMN-16455",
    "OMN-16561",
    "OMN-16775",
)
_CREDENTIAL_DEFECT_TICKET = "OMN-14993"


def _fresh(ticket_id: str) -> ModelDodVerifyRetryState:
    return ModelDodVerifyRetryState(ticket_id=ticket_id, attempts=())


# ---------------------------------------------------------------------------
# DoD 1 — a typed terminal unresolved status, distinguishable from never-started
# ---------------------------------------------------------------------------


class TestTerminalUnresolvedStatus:
    def test_enum_carries_an_unresolved_member(self) -> None:
        assert EnumDodVerifyStatus.UNRESOLVED.value == "unresolved"
        assert EnumDodVerifyStatus.UNRESOLVED is not EnumDodVerifyStatus.PENDING

    def test_unresolved_state_requires_a_typed_cause(self) -> None:
        """UNRESOLVED without a cause would be the untyped label all over again."""
        with pytest.raises(ValidationError):
            ModelDodVerifyState(
                correlation_id="00000000-0000-0000-0000-000000000001",
                ticket_id="OMN-1",
                status=EnumDodVerifyStatus.UNRESOLVED,
            )

    def test_a_cause_may_not_ride_on_a_resolved_status(self) -> None:
        with pytest.raises(ValidationError):
            ModelDodVerifyState(
                correlation_id="00000000-0000-0000-0000-000000000001",
                ticket_id="OMN-1",
                status=EnumDodVerifyStatus.VERIFIED,
                unresolved_cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
            )

    def test_never_started_and_timed_out_are_distinguishable(self) -> None:
        never_started = _fresh("OMN-16690")
        timed_out = never_started.start_attempt(now=_T0).complete_attempt(
            status=EnumDodVerifyStatus.UNRESOLVED,
            cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
            now=_T0 + timedelta(seconds=5),
            detail="caller-side timeout",
        )
        assert never_started.status is EnumDodVerifyStatus.PENDING
        assert timed_out.status is EnumDodVerifyStatus.UNRESOLVED
        assert timed_out.latest_cause is (
            EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT
        )


# ---------------------------------------------------------------------------
# DoD 2 — RUN_ERROR_OR_TIMEOUT is a typed code
# ---------------------------------------------------------------------------


class TestTypedCauseTaxonomy:
    def test_run_error_or_timeout_is_a_real_enum_member(self) -> None:
        assert (
            EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT.value
            == "run_error_or_timeout"
        )

    def test_retry_eligibility_is_encoded_on_the_member(self) -> None:
        assert EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT.retry_eligible is True
        assert EnumDodVerifyUnresolvedCause.PR_LOOKUP_FAILED.retry_eligible is False
        assert EnumDodVerifyUnresolvedCause.PR_LOOKUP_AMBIGUOUS.retry_eligible is False
        assert EnumDodVerifyUnresolvedCause.REPO_LOOKUP_FAILED.retry_eligible is False
        assert EnumDodVerifyUnresolvedCause.UNKNOWN.retry_eligible is False

    def test_existing_typed_github_error_codes_map_without_invention(self) -> None:
        """The codes the github effect handler already emits, mapped as-is."""
        for code, expected in (
            ("PR_LOOKUP_FAILED", EnumDodVerifyUnresolvedCause.PR_LOOKUP_FAILED),
            ("PR_LOOKUP_AMBIGUOUS", EnumDodVerifyUnresolvedCause.PR_LOOKUP_AMBIGUOUS),
            ("REPO_LOOKUP_FAILED", EnumDodVerifyUnresolvedCause.REPO_LOOKUP_FAILED),
        ):
            assert EnumDodVerifyUnresolvedCause.from_error_code(code) is expected

    def test_an_unrecognised_code_fails_closed_to_unknown(self) -> None:
        cause = EnumDodVerifyUnresolvedCause.from_error_code("SOMETHING_NEW")
        assert cause is EnumDodVerifyUnresolvedCause.UNKNOWN
        assert cause.retry_eligible is False

    def test_a_prefixed_message_still_resolves_to_its_code(self) -> None:
        """``_last_pr_lookup_error`` carries ``CODE: detail``, not a bare code."""
        assert (
            EnumDodVerifyUnresolvedCause.from_error_code(
                "PR_LOOKUP_FAILED: cannot resolve target repo for PR search"
            )
            is EnumDodVerifyUnresolvedCause.PR_LOOKUP_FAILED
        )


# ---------------------------------------------------------------------------
# DoD 3 — bounded backoff for the timeout class only
# ---------------------------------------------------------------------------


class TestBoundedBackoff:
    def test_delay_grows_and_is_capped(self) -> None:
        policy = ModelDodVerifyRetryPolicy(
            max_attempts=5,
            base_delay_seconds=10.0,
            multiplier=4.0,
            max_delay_seconds=100.0,
        )
        assert policy.delay_for_attempt(1) == 10.0
        assert policy.delay_for_attempt(2) == 40.0
        assert policy.delay_for_attempt(3) == 100.0  # capped, not 160
        assert policy.delay_for_attempt(4) == 100.0

    def test_policy_rejects_a_cap_below_the_base_delay(self) -> None:
        with pytest.raises(ValidationError):
            ModelDodVerifyRetryPolicy(
                max_attempts=3,
                base_delay_seconds=100.0,
                multiplier=2.0,
                max_delay_seconds=10.0,
            )

    def test_a_never_attempted_item_is_attempted_now(self) -> None:
        decision = plan_next_attempt(
            _fresh("OMN-16690"), policy=CANONICAL_DOD_VERIFY_RETRY_POLICY, now=_T0
        )
        assert decision.disposition is EnumDodVerifyRetryDisposition.ATTEMPT_NOW

    def test_a_timeout_schedules_a_bounded_retry(self) -> None:
        state = (
            _fresh("OMN-16690")
            .start_attempt(now=_T0)
            .complete_attempt(
                status=EnumDodVerifyStatus.UNRESOLVED,
                cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
                now=_T0,
                detail="timeout",
            )
        )
        decision = plan_next_attempt(
            state, policy=CANONICAL_DOD_VERIFY_RETRY_POLICY, now=_T0
        )
        assert decision.disposition is EnumDodVerifyRetryDisposition.RETRY_SCHEDULED
        assert decision.next_attempt_not_before == _T0 + timedelta(
            seconds=CANONICAL_DOD_VERIFY_RETRY_POLICY.delay_for_attempt(1)
        )

    def test_the_backoff_window_gates_the_next_attempt(self) -> None:
        state = (
            _fresh("OMN-16690")
            .start_attempt(now=_T0)
            .complete_attempt(
                status=EnumDodVerifyStatus.UNRESOLVED,
                cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
                now=_T0,
                detail="timeout",
            )
        )
        delay = CANONICAL_DOD_VERIFY_RETRY_POLICY.delay_for_attempt(1)
        assert (
            plan_next_attempt(
                state,
                policy=CANONICAL_DOD_VERIFY_RETRY_POLICY,
                now=_T0 + timedelta(seconds=delay),
            ).disposition
            is EnumDodVerifyRetryDisposition.ATTEMPT_NOW
        )

    def test_retries_are_bounded_and_terminate(self) -> None:
        policy = ModelDodVerifyRetryPolicy(
            max_attempts=3,
            base_delay_seconds=1.0,
            multiplier=2.0,
            max_delay_seconds=10.0,
        )
        state = _fresh("OMN-16690")
        for i in range(3):
            state = state.start_attempt(
                now=_T0 + timedelta(minutes=i)
            ).complete_attempt(
                status=EnumDodVerifyStatus.UNRESOLVED,
                cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
                now=_T0 + timedelta(minutes=i),
                detail="timeout",
            )
        decision = plan_next_attempt(state, policy=policy, now=_T0 + timedelta(days=1))
        assert (
            decision.disposition
            is EnumDodVerifyRetryDisposition.TERMINAL_ATTEMPTS_EXHAUSTED
        )
        assert decision.next_attempt_not_before is None

    def test_a_credential_defect_is_never_retried(self) -> None:
        """OMN-14993's class. Retrying reproduces it exactly, so it is refused
        on the FIRST observation — not after burning the attempt budget."""
        state = (
            _fresh(_CREDENTIAL_DEFECT_TICKET)
            .start_attempt(now=_T0)
            .complete_attempt(
                status=EnumDodVerifyStatus.UNRESOLVED,
                cause=EnumDodVerifyUnresolvedCause.PR_LOOKUP_FAILED,
                now=_T0,
                detail="cannot resolve target repo for PR search",
            )
        )
        decision = plan_next_attempt(
            state, policy=CANONICAL_DOD_VERIFY_RETRY_POLICY, now=_T0
        )
        assert (
            decision.disposition is EnumDodVerifyRetryDisposition.TERMINAL_NOT_RETRYABLE
        )
        assert decision.next_attempt_not_before is None
        assert decision.attempt_count == 1

    def test_the_ten_held_items_split_nine_to_one(self) -> None:
        """The ticket's falsifiable done-proof, as a deterministic assertion."""
        dispositions: dict[str, EnumDodVerifyRetryDisposition] = {}
        for tid in _NINE_TIMED_OUT:
            state = (
                _fresh(tid)
                .start_attempt(now=_T0)
                .complete_attempt(
                    status=EnumDodVerifyStatus.UNRESOLVED,
                    cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
                    now=_T0,
                    detail="run error or timeout",
                )
            )
            dispositions[tid] = plan_next_attempt(
                state, policy=CANONICAL_DOD_VERIFY_RETRY_POLICY, now=_T0
            ).disposition
        credential = (
            _fresh(_CREDENTIAL_DEFECT_TICKET)
            .start_attempt(now=_T0)
            .complete_attempt(
                status=EnumDodVerifyStatus.UNRESOLVED,
                cause=EnumDodVerifyUnresolvedCause.PR_LOOKUP_FAILED,
                now=_T0,
                detail="PR_LOOKUP_FAILED",
            )
        )
        dispositions[_CREDENTIAL_DEFECT_TICKET] = plan_next_attempt(
            credential, policy=CANONICAL_DOD_VERIFY_RETRY_POLICY, now=_T0
        ).disposition

        assert len(dispositions) == 10
        backoff = [
            t
            for t, d in dispositions.items()
            if d is EnumDodVerifyRetryDisposition.RETRY_SCHEDULED
        ]
        refused = [
            t
            for t, d in dispositions.items()
            if d is EnumDodVerifyRetryDisposition.TERMINAL_NOT_RETRYABLE
        ]
        assert sorted(backoff) == sorted(_NINE_TIMED_OUT)
        assert refused == [_CREDENTIAL_DEFECT_TICKET]


# ---------------------------------------------------------------------------
# DoD 4 — an unresolved item never decays back to PENDING
# ---------------------------------------------------------------------------


class TestNoDecayToPending:
    def test_pending_is_not_a_recordable_attempt_outcome(self) -> None:
        """PENDING means "no attempt exists". Once one does, it is unreachable —
        enforced structurally so no producer can write the regression."""
        state = _fresh("OMN-16690").start_attempt(now=_T0)
        with pytest.raises(ValueError, match="not a recordable attempt outcome"):
            state.complete_attempt(
                status=EnumDodVerifyStatus.PENDING,
                cause=None,
                now=_T0,
                detail="",
            )

    def test_a_state_with_attempts_never_reads_pending(self) -> None:
        state = (
            _fresh("OMN-16690")
            .start_attempt(now=_T0)
            .complete_attempt(
                status=EnumDodVerifyStatus.UNRESOLVED,
                cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
                now=_T0,
                detail="timeout",
            )
        )
        assert state.status is not EnumDodVerifyStatus.PENDING

    def test_an_abandoned_attempt_reads_unresolved_not_pending(self) -> None:
        """The process died mid-attempt: no completion was ever written. It
        must fail CLOSED to unresolved, never read as "not yet attempted"."""
        state = _fresh("OMN-16690").start_attempt(now=_T0)
        assert state.has_abandoned_attempt is True
        assert state.status is EnumDodVerifyStatus.UNRESOLVED

    def test_reconciling_an_abandoned_attempt_types_it_as_a_timeout(self) -> None:
        state = _fresh("OMN-16690").start_attempt(now=_T0)
        reconciled = reconcile_abandoned_attempt(
            state, now=_T0 + timedelta(hours=1), detail="no completion recorded"
        )
        assert reconciled.has_abandoned_attempt is False
        assert reconciled.status is EnumDodVerifyStatus.UNRESOLVED
        assert (
            reconciled.latest_cause is EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT
        )
        assert reconciled.attempt_count == 1

    def test_reconciling_a_clean_state_is_a_no_op(self) -> None:
        state = _fresh("OMN-16690")
        assert reconcile_abandoned_attempt(state, now=_T0, detail="x") == state


# ---------------------------------------------------------------------------
# Durable per-item persistence
# ---------------------------------------------------------------------------


class TestRetryLedger:
    def test_absent_record_reads_none_not_a_fabricated_pending(
        self, tmp_path: Path
    ) -> None:
        ledger = FilesystemDodVerifyRetryLedger(ticket_state_root=tmp_path)
        assert ledger.read("OMN-16690") is None

    def test_round_trips_per_item(self, tmp_path: Path) -> None:
        ledger = FilesystemDodVerifyRetryLedger(ticket_state_root=tmp_path)
        state = (
            _fresh("OMN-16690")
            .start_attempt(now=_T0)
            .complete_attempt(
                status=EnumDodVerifyStatus.UNRESOLVED,
                cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
                now=_T0,
                detail="timeout",
            )
        )
        ledger.write(state)
        assert ledger.read("OMN-16690") == state

    def test_a_second_item_does_not_clobber_the_first(self, tmp_path: Path) -> None:
        ledger = FilesystemDodVerifyRetryLedger(ticket_state_root=tmp_path)
        for tid in ("OMN-16690", "OMN-16785"):
            ledger.write(
                _fresh(tid)
                .start_attempt(now=_T0)
                .complete_attempt(
                    status=EnumDodVerifyStatus.UNRESOLVED,
                    cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
                    now=_T0,
                    detail="timeout",
                )
            )
        assert ledger.read("OMN-16690") is not None
        assert ledger.read("OMN-16785") is not None

    def test_write_refuses_to_truncate_recorded_history(self, tmp_path: Path) -> None:
        """The durable arm of DoD 4: a fresh run must not overwrite a recorded
        unresolved item with an empty (PENDING-reading) record."""
        ledger = FilesystemDodVerifyRetryLedger(ticket_state_root=tmp_path)
        recorded = (
            _fresh("OMN-16690")
            .start_attempt(now=_T0)
            .complete_attempt(
                status=EnumDodVerifyStatus.UNRESOLVED,
                cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
                now=_T0,
                detail="timeout",
            )
        )
        ledger.write(recorded)
        with pytest.raises(ValueError, match="refusing to truncate"):
            ledger.write(_fresh("OMN-16690"))
        assert ledger.read("OMN-16690") == recorded

    def test_write_refuses_a_rewritten_prefix(self, tmp_path: Path) -> None:
        ledger = FilesystemDodVerifyRetryLedger(ticket_state_root=tmp_path)
        recorded = (
            _fresh("OMN-16690")
            .start_attempt(now=_T0)
            .complete_attempt(
                status=EnumDodVerifyStatus.UNRESOLVED,
                cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
                now=_T0,
                detail="timeout",
            )
        )
        ledger.write(recorded)
        forged = (
            _fresh("OMN-16690")
            .start_attempt(now=_T0)
            .complete_attempt(
                status=EnumDodVerifyStatus.VERIFIED,
                cause=None,
                now=_T0,
                detail="forged",
            )
        )
        with pytest.raises(ValueError, match="retry history is append-only"):
            ledger.write(forged)

    def test_append_extends_recorded_history(self, tmp_path: Path) -> None:
        ledger = FilesystemDodVerifyRetryLedger(ticket_state_root=tmp_path)
        first = (
            _fresh("OMN-16690")
            .start_attempt(now=_T0)
            .complete_attempt(
                status=EnumDodVerifyStatus.UNRESOLVED,
                cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
                now=_T0,
                detail="timeout",
            )
        )
        ledger.write(first)
        second = first.start_attempt(now=_T0 + timedelta(hours=1)).complete_attempt(
            status=EnumDodVerifyStatus.VERIFIED,
            cause=None,
            now=_T0 + timedelta(hours=1),
            detail="",
        )
        ledger.write(second)
        read_back = ledger.read("OMN-16690")
        assert read_back is not None
        assert read_back.attempt_count == 2
        assert read_back.status is EnumDodVerifyStatus.VERIFIED

    def test_list_unresolved_names_exactly_the_held_items(self, tmp_path: Path) -> None:
        ledger = FilesystemDodVerifyRetryLedger(ticket_state_root=tmp_path)
        for tid in _NINE_TIMED_OUT:
            ledger.write(
                _fresh(tid)
                .start_attempt(now=_T0)
                .complete_attempt(
                    status=EnumDodVerifyStatus.UNRESOLVED,
                    cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
                    now=_T0,
                    detail="timeout",
                )
            )
        ledger.write(
            _fresh(_CREDENTIAL_DEFECT_TICKET)
            .start_attempt(now=_T0)
            .complete_attempt(
                status=EnumDodVerifyStatus.UNRESOLVED,
                cause=EnumDodVerifyUnresolvedCause.PR_LOOKUP_FAILED,
                now=_T0,
                detail="PR_LOOKUP_FAILED",
            )
        )
        ledger.write(
            _fresh("OMN-99999")
            .start_attempt(now=_T0)
            .complete_attempt(
                status=EnumDodVerifyStatus.VERIFIED,
                cause=None,
                now=_T0,
                detail="",
            )
        )
        unresolved = {s.ticket_id for s in ledger.list_unresolved()}
        assert unresolved == set(_NINE_TIMED_OUT) | {_CREDENTIAL_DEFECT_TICKET}
