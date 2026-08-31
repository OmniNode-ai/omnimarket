# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17022 — the sweep reconciles per item instead of re-running the audit.

The ticket's falsifiable done-proof, exercised against the real handler with
the ``gh`` boundary and the clock injected: re-run the sweep against exactly
the ten items the 2026-08-29 sprint-triage closeout held, and prove

* nine take the bounded-backoff path,
* OMN-14993 takes the credential-defect path and is never retried,
* a forced timeout lands in a terminal unresolved state that is observably
  distinct from an item that was never attempted.

The causes are read back out of the durable per-item ledger the sweep writes —
not out of a markdown table. The closeout doc is where the untyped labels came
from; it is not an authority any of this consults.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)
from omnimarket.events.dod_verify_retry import (
    EnumDodVerifyRetryDisposition,
    ModelDodVerifyRetryPolicy,
)
from omnimarket.nodes.node_dod_sweep_orchestrator.handlers.handler_dod_sweep_orchestrator import (
    HandlerDodSweepOrchestrator,
)
from omnimarket.nodes.node_dod_sweep_orchestrator.models.model_dod_sweep_orchestrator_request import (
    ModelDodSweepOrchestratorRequest,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
)
from omnimarket.protocols.protocol_dod_verify_retry_ledger import (
    FilesystemDodVerifyRetryLedger,
)

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)

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
_TEN_HELD: tuple[str, ...] = (*_NINE_TIMED_OUT, _CREDENTIAL_DEFECT_TICKET)

_POLICY = ModelDodVerifyRetryPolicy(
    max_attempts=3,
    base_delay_seconds=60.0,
    multiplier=2.0,
    max_delay_seconds=600.0,
)


class _Clock:
    """A hand-advanced clock, so backoff is proven rather than waited out."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now = self.now + timedelta(**kwargs)


def _contract_root(tmp_path: Path, tickets: tuple[str, ...]) -> Path:
    """A contract dir with one real contract per ticket, so contract_exists passes."""
    root = tmp_path / "occ"
    (root / "contracts").mkdir(parents=True)
    for tid in tickets:
        (root / "contracts" / f"{tid}.yaml").write_text(
            f'ticket_id: "{tid}"\n', encoding="utf-8"
        )
    return root


def _never_reached_checks(pr_number: str, repo: str) -> tuple[bool, str]:
    """ci_green is never enabled in these fixtures; this seam must not fire."""
    raise AssertionError(f"gh_pr_checks_pass_fn was called for {repo}#{pr_number}")


def _find_pr_raising_for(timeout_tickets: frozenset[str]):
    """gh seam that dies on the nine and finds nothing for the credential item."""

    def _fn(ticket_id: str, repos: tuple[str, ...]) -> dict[str, str]:
        if ticket_id in timeout_tickets:
            raise TimeoutError(f"gh pr list timed out for {ticket_id}")
        return {}

    return _fn


def _handler(
    *,
    evidence_root: Path,
    clock: _Clock,
    timeout_tickets: frozenset[str],
) -> HandlerDodSweepOrchestrator:
    return HandlerDodSweepOrchestrator(
        gh_find_merged_pr_fn=_find_pr_raising_for(timeout_tickets),
        gh_pr_checks_pass_fn=_never_reached_checks,
        clock_fn=clock,
        retry_ledger_factory=lambda root: FilesystemDodVerifyRetryLedger(
            ticket_state_root=root / ".evidence"
        ),
    )


def _request(
    *, contract_root: Path, evidence_root: Path, tickets: tuple[str, ...]
) -> ModelDodSweepOrchestratorRequest:
    return ModelDodSweepOrchestratorRequest(
        scope="beta",
        ticket_ids=tickets,
        contract_root=str(contract_root),
        evidence_root=str(evidence_root),
        gh_repos=("OmniNode-ai/omnimarket",),
        enabled_checks=("contract_exists", "pr_merged"),
        retry_policy=_POLICY,
    )


class TestTheTenHeldItems:
    def test_first_pass_records_a_typed_unresolved_cause_per_item(
        self, tmp_path: Path
    ) -> None:
        clock = _Clock(_T0)
        contract_root = _contract_root(tmp_path, _TEN_HELD)
        evidence_root = tmp_path / "evidence"
        handler = _handler(
            evidence_root=evidence_root,
            clock=clock,
            timeout_tickets=frozenset(_NINE_TIMED_OUT),
        )

        result = handler.handle(
            _request(
                contract_root=contract_root,
                evidence_root=evidence_root,
                tickets=_TEN_HELD,
            )
        )

        by_ticket = {r.ticket_id: r for r in result.batch_results}
        assert len(by_ticket) == 10
        for tid in _NINE_TIMED_OUT:
            assert by_ticket[tid].status == "unresolved"
            assert (
                by_ticket[tid].unresolved_cause
                is EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT
            )
        # The credential item's lookup did not fault — it returned no PR. That
        # is the sweep's ordinary "no merged PR" red, not a run fault, so it is
        # NOT laundered into the retry class.
        assert by_ticket[_CREDENTIAL_DEFECT_TICKET].status == "failed"
        assert result.batch_unresolved == 9

    def test_a_run_fault_is_persisted_per_item(self, tmp_path: Path) -> None:
        clock = _Clock(_T0)
        contract_root = _contract_root(tmp_path, _TEN_HELD)
        evidence_root = tmp_path / "evidence"
        handler = _handler(
            evidence_root=evidence_root,
            clock=clock,
            timeout_tickets=frozenset(_NINE_TIMED_OUT),
        )
        handler.handle(
            _request(
                contract_root=contract_root,
                evidence_root=evidence_root,
                tickets=_TEN_HELD,
            )
        )

        ledger = FilesystemDodVerifyRetryLedger(
            ticket_state_root=evidence_root / ".evidence"
        )
        for tid in _NINE_TIMED_OUT:
            state = ledger.read(tid)
            assert state is not None, tid
            assert state.status is EnumDodVerifyStatus.UNRESOLVED
            assert (
                state.latest_cause is EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT
            )
        assert {s.ticket_id for s in ledger.list_unresolved()} == set(_NINE_TIMED_OUT)

    def test_second_pass_inside_the_window_holds_the_nine_in_backoff(
        self, tmp_path: Path
    ) -> None:
        clock = _Clock(_T0)
        contract_root = _contract_root(tmp_path, _TEN_HELD)
        evidence_root = tmp_path / "evidence"
        handler = _handler(
            evidence_root=evidence_root,
            clock=clock,
            timeout_tickets=frozenset(_NINE_TIMED_OUT),
        )
        request = _request(
            contract_root=contract_root,
            evidence_root=evidence_root,
            tickets=_TEN_HELD,
        )
        handler.handle(request)

        clock.advance(seconds=1)
        second = handler.handle(request)
        by_ticket = {r.ticket_id: r for r in second.batch_results}
        for tid in _NINE_TIMED_OUT:
            assert (
                by_ticket[tid].retry_disposition
                is EnumDodVerifyRetryDisposition.RETRY_SCHEDULED
            ), tid
            assert by_ticket[tid].next_attempt_not_before != ""

        ledger = FilesystemDodVerifyRetryLedger(
            ticket_state_root=evidence_root / ".evidence"
        )
        state = ledger.read("OMN-16690")
        assert state is not None
        # Held, not re-run: still exactly one attempt on the record.
        assert state.attempt_count == 1

    def test_advancing_past_the_window_re_attempts_and_bounds_at_max(
        self, tmp_path: Path
    ) -> None:
        clock = _Clock(_T0)
        contract_root = _contract_root(tmp_path, _TEN_HELD)
        evidence_root = tmp_path / "evidence"
        handler = _handler(
            evidence_root=evidence_root,
            clock=clock,
            timeout_tickets=frozenset(_NINE_TIMED_OUT),
        )
        request = _request(
            contract_root=contract_root,
            evidence_root=evidence_root,
            tickets=_TEN_HELD,
        )
        for _ in range(_POLICY.max_attempts):
            handler.handle(request)
            clock.advance(hours=1)

        final = handler.handle(request)
        by_ticket = {r.ticket_id: r for r in final.batch_results}
        for tid in _NINE_TIMED_OUT:
            assert (
                by_ticket[tid].retry_disposition
                is EnumDodVerifyRetryDisposition.TERMINAL_ATTEMPTS_EXHAUSTED
            ), tid

        ledger = FilesystemDodVerifyRetryLedger(
            ticket_state_root=evidence_root / ".evidence"
        )
        state = ledger.read("OMN-16690")
        assert state is not None
        assert state.attempt_count == _POLICY.max_attempts

    def test_a_credential_defect_is_refused_a_retry(self, tmp_path: Path) -> None:
        """OMN-14993's path. Seeded through the ledger as a recorded
        PR_LOOKUP_FAILED — the shape node_dod_verify writes for it — then
        re-swept. It must be refused on the FIRST re-run, not after the
        budget is spent, and it must never be re-executed."""
        clock = _Clock(_T0)
        contract_root = _contract_root(tmp_path, _TEN_HELD)
        evidence_root = tmp_path / "evidence"
        ledger = FilesystemDodVerifyRetryLedger(
            ticket_state_root=evidence_root / ".evidence"
        )
        from omnimarket.events.dod_verify_retry import (
            ModelDodVerifyRetryState,
        )

        ledger.write(
            ModelDodVerifyRetryState(ticket_id=_CREDENTIAL_DEFECT_TICKET, attempts=())
            .start_attempt(now=_T0 - timedelta(days=1))
            .complete_attempt(
                status=EnumDodVerifyStatus.UNRESOLVED,
                cause=EnumDodVerifyUnresolvedCause.PR_LOOKUP_FAILED,
                now=_T0 - timedelta(days=1),
                detail="cannot resolve target repo for PR search",
                error_code="PR_LOOKUP_FAILED",
            )
        )

        executed: list[str] = []

        def _tracking_find(ticket_id: str, repos: tuple[str, ...]) -> dict[str, str]:
            executed.append(ticket_id)
            return {}

        handler = HandlerDodSweepOrchestrator(
            gh_find_merged_pr_fn=_tracking_find,
            gh_pr_checks_pass_fn=_never_reached_checks,
            clock_fn=clock,
            retry_ledger_factory=lambda root: FilesystemDodVerifyRetryLedger(
                ticket_state_root=root / ".evidence"
            ),
        )
        result = handler.handle(
            _request(
                contract_root=contract_root,
                evidence_root=evidence_root,
                tickets=(_CREDENTIAL_DEFECT_TICKET,),
            )
        )

        only = result.batch_results[0]
        assert only.status == "unresolved"
        assert (
            only.retry_disposition
            is EnumDodVerifyRetryDisposition.TERMINAL_NOT_RETRYABLE
        )
        assert only.unresolved_cause is EnumDodVerifyUnresolvedCause.PR_LOOKUP_FAILED
        assert only.next_attempt_not_before == ""
        assert executed == [], "a credential defect must not be re-executed"
        assert ledger.read(_CREDENTIAL_DEFECT_TICKET) is not None
        state = ledger.read(_CREDENTIAL_DEFECT_TICKET)
        assert state is not None
        assert state.attempt_count == 1, "no attempt may be spent on a refusal"

    def test_the_ten_split_nine_to_one_on_the_reconciliation_pass(
        self, tmp_path: Path
    ) -> None:
        """The done-proof itself, as one assertion over all ten."""
        clock = _Clock(_T0)
        contract_root = _contract_root(tmp_path, _TEN_HELD)
        evidence_root = tmp_path / "evidence"
        ledger = FilesystemDodVerifyRetryLedger(
            ticket_state_root=evidence_root / ".evidence"
        )
        from omnimarket.events.dod_verify_retry import (
            ModelDodVerifyRetryState,
        )

        # Seed each item with the outcome its producer actually recorded.
        for tid in _NINE_TIMED_OUT:
            ledger.write(
                ModelDodVerifyRetryState(ticket_id=tid, attempts=())
                .start_attempt(now=_T0)
                .complete_attempt(
                    status=EnumDodVerifyStatus.UNRESOLVED,
                    cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
                    now=_T0,
                    detail="run error or timeout",
                )
            )
        ledger.write(
            ModelDodVerifyRetryState(ticket_id=_CREDENTIAL_DEFECT_TICKET, attempts=())
            .start_attempt(now=_T0)
            .complete_attempt(
                status=EnumDodVerifyStatus.UNRESOLVED,
                cause=EnumDodVerifyUnresolvedCause.PR_LOOKUP_FAILED,
                now=_T0,
                detail="PR_LOOKUP_FAILED",
                error_code="PR_LOOKUP_FAILED",
            )
        )

        handler = _handler(
            evidence_root=evidence_root, clock=clock, timeout_tickets=frozenset()
        )
        result = handler.handle(
            _request(
                contract_root=contract_root,
                evidence_root=evidence_root,
                tickets=_TEN_HELD,
            )
        )

        by_ticket = {r.ticket_id: r for r in result.batch_results}
        backoff = sorted(
            tid
            for tid, r in by_ticket.items()
            if r.retry_disposition is EnumDodVerifyRetryDisposition.RETRY_SCHEDULED
        )
        refused = sorted(
            tid
            for tid, r in by_ticket.items()
            if r.retry_disposition
            is EnumDodVerifyRetryDisposition.TERMINAL_NOT_RETRYABLE
        )
        assert backoff == sorted(_NINE_TIMED_OUT)
        assert refused == [_CREDENTIAL_DEFECT_TICKET]


class TestForcedTimeoutIsDistinctFromNeverStarted:
    def test_the_in_flight_marker_is_durable_before_the_run_executes(
        self, tmp_path: Path
    ) -> None:
        """The ordering claim, proven from inside the run itself.

        A hard kill (SIGKILL, host loss) executes no Python, so the ONLY thing
        that can distinguish "was attempted and died" from "never attempted" is
        a record written *before* the work starts. This asserts it is already
        on disk while the checks are still running.
        """
        clock = _Clock(_T0)
        contract_root = _contract_root(tmp_path, ("OMN-16690",))
        evidence_root = tmp_path / "evidence"
        observed: list[bool] = []

        def _observe(ticket_id: str, repos: tuple[str, ...]) -> dict[str, str]:
            mid_run = FilesystemDodVerifyRetryLedger(
                ticket_state_root=evidence_root / ".evidence"
            ).read(ticket_id)
            observed.append(mid_run is not None and mid_run.has_abandoned_attempt)
            return {}

        handler = HandlerDodSweepOrchestrator(
            gh_find_merged_pr_fn=_observe,
            gh_pr_checks_pass_fn=_never_reached_checks,
            clock_fn=clock,
            retry_ledger_factory=lambda root: FilesystemDodVerifyRetryLedger(
                ticket_state_root=root / ".evidence"
            ),
        )
        handler.handle(
            _request(
                contract_root=contract_root,
                evidence_root=evidence_root,
                tickets=("OMN-16690",),
            )
        )
        assert observed == [True]

    def test_an_in_process_kill_is_typed_and_re_raised(self, tmp_path: Path) -> None:
        """A SystemExit (the shape a stopped lane presents in-process) is
        recorded with a typed cause before the exception propagates, and the
        item that never ran keeps NO record at all."""
        clock = _Clock(_T0)
        contract_root = _contract_root(tmp_path, ("OMN-16690", "OMN-16785"))
        evidence_root = tmp_path / "evidence"

        def _dies(ticket_id: str, repos: tuple[str, ...]) -> dict[str, str]:
            raise SystemExit(143)

        handler = HandlerDodSweepOrchestrator(
            gh_find_merged_pr_fn=_dies,
            gh_pr_checks_pass_fn=_never_reached_checks,
            clock_fn=clock,
            retry_ledger_factory=lambda root: FilesystemDodVerifyRetryLedger(
                ticket_state_root=root / ".evidence"
            ),
        )
        with pytest.raises(SystemExit):
            handler.handle(
                _request(
                    contract_root=contract_root,
                    evidence_root=evidence_root,
                    tickets=("OMN-16690", "OMN-16785"),
                )
            )

        ledger = FilesystemDodVerifyRetryLedger(
            ticket_state_root=evidence_root / ".evidence"
        )
        killed = ledger.read("OMN-16690")
        assert killed is not None
        assert killed.status is EnumDodVerifyStatus.UNRESOLVED
        assert killed.latest_cause is (
            EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT
        )
        # The whole point: the item that never ran has NO record, and the one
        # that was killed reads UNRESOLVED — neither of them reads PENDING.
        assert ledger.read("OMN-16785") is None

    def test_a_hard_kill_reads_unresolved_not_pending(self, tmp_path: Path) -> None:
        """SIGKILL runs no Python at all, so the record stays in-flight. It
        must fail CLOSED to unresolved rather than read as never-attempted."""
        evidence_root = tmp_path / "evidence"
        ledger = FilesystemDodVerifyRetryLedger(
            ticket_state_root=evidence_root / ".evidence"
        )
        from omnimarket.events.dod_verify_retry import (
            ModelDodVerifyRetryState,
        )

        ledger.write(
            ModelDodVerifyRetryState(ticket_id="OMN-16690", attempts=()).start_attempt(
                now=_T0
            )
        )
        killed = ledger.read("OMN-16690")
        assert killed is not None
        assert killed.has_abandoned_attempt is True
        assert killed.status is EnumDodVerifyStatus.UNRESOLVED
        assert ledger.read("OMN-16785") is None

    def test_the_next_pass_types_the_abandoned_attempt_and_retries_it(
        self, tmp_path: Path
    ) -> None:
        clock = _Clock(_T0)
        contract_root = _contract_root(tmp_path, ("OMN-16690",))
        evidence_root = tmp_path / "evidence"
        ledger = FilesystemDodVerifyRetryLedger(
            ticket_state_root=evidence_root / ".evidence"
        )
        from omnimarket.events.dod_verify_retry import (
            ModelDodVerifyRetryState,
        )

        ledger.write(
            ModelDodVerifyRetryState(ticket_id="OMN-16690", attempts=()).start_attempt(
                now=_T0 - timedelta(hours=2)
            )
        )

        survivor = _handler(
            evidence_root=evidence_root, clock=clock, timeout_tickets=frozenset()
        )
        survivor.handle(
            _request(
                contract_root=contract_root,
                evidence_root=evidence_root,
                tickets=("OMN-16690",),
            )
        )

        state = ledger.read("OMN-16690")
        assert state is not None
        assert state.has_abandoned_attempt is False
        assert state.attempts[0].cause is (
            EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT
        )
        # Backoff had long elapsed, so the reconciled item was re-attempted
        # rather than being stranded the way the ten held items were.
        assert state.attempt_count == 2


class TestForceRetryIsTheOperatorLever:
    def test_force_retry_re_attempts_an_exhausted_item_without_erasing_history(
        self, tmp_path: Path
    ) -> None:
        clock = _Clock(_T0)
        contract_root = _contract_root(tmp_path, ("OMN-16690",))
        evidence_root = tmp_path / "evidence"
        ledger = FilesystemDodVerifyRetryLedger(
            ticket_state_root=evidence_root / ".evidence"
        )
        from omnimarket.events.dod_verify_retry import (
            ModelDodVerifyRetryState,
        )

        state = ModelDodVerifyRetryState(ticket_id="OMN-16690", attempts=())
        for i in range(_POLICY.max_attempts):
            state = state.start_attempt(
                now=_T0 - timedelta(days=1) + timedelta(minutes=i)
            ).complete_attempt(
                status=EnumDodVerifyStatus.UNRESOLVED,
                cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
                now=_T0 - timedelta(days=1) + timedelta(minutes=i),
                detail="timeout",
            )
        ledger.write(state)

        handler = _handler(
            evidence_root=evidence_root, clock=clock, timeout_tickets=frozenset()
        )
        request = _request(
            contract_root=contract_root,
            evidence_root=evidence_root,
            tickets=("OMN-16690",),
        ).model_copy(update={"force_retry": True})
        handler.handle(request)

        after = ledger.read("OMN-16690")
        assert after is not None
        assert after.attempt_count == _POLICY.max_attempts + 1
        assert after.attempts[0].cause is (
            EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT
        )

    def test_force_retry_still_refuses_a_credential_defect(
        self, tmp_path: Path
    ) -> None:
        """The lever overrides the BUDGET, never the taxonomy. Retrying a
        resolution defect reproduces it exactly, forced or not."""
        clock = _Clock(_T0)
        contract_root = _contract_root(tmp_path, (_CREDENTIAL_DEFECT_TICKET,))
        evidence_root = tmp_path / "evidence"
        ledger = FilesystemDodVerifyRetryLedger(
            ticket_state_root=evidence_root / ".evidence"
        )
        from omnimarket.events.dod_verify_retry import (
            ModelDodVerifyRetryState,
        )

        ledger.write(
            ModelDodVerifyRetryState(ticket_id=_CREDENTIAL_DEFECT_TICKET, attempts=())
            .start_attempt(now=_T0 - timedelta(days=1))
            .complete_attempt(
                status=EnumDodVerifyStatus.UNRESOLVED,
                cause=EnumDodVerifyUnresolvedCause.PR_LOOKUP_FAILED,
                now=_T0 - timedelta(days=1),
                detail="PR_LOOKUP_FAILED",
            )
        )
        handler = _handler(
            evidence_root=evidence_root, clock=clock, timeout_tickets=frozenset()
        )
        result = handler.handle(
            _request(
                contract_root=contract_root,
                evidence_root=evidence_root,
                tickets=(_CREDENTIAL_DEFECT_TICKET,),
            ).model_copy(update={"force_retry": True})
        )
        assert (
            result.batch_results[0].retry_disposition
            is EnumDodVerifyRetryDisposition.TERMINAL_NOT_RETRYABLE
        )
