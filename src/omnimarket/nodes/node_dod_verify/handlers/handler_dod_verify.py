"""HandlerDodVerify — DoD evidence verification compute node.

Simple compute: load contract -> run evidence checks -> emit report.
Not a multi-phase FSM — single-shot computation.

When callers provide pre-collected ``evidence_results``, the handler is pure
(no I/O). When ``evidence_results`` is None, the handler uses
EvidenceCollector to load the ticket contract and run checks — this is the
primary execution path for RuntimeLocal and onex run-node invocations.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass
from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_completed_event import (
    ModelDodVerifyCompletedEvent,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    EnumOccRefRefreshOutcome,
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
        EvidenceCollector,
    )

logger = logging.getLogger(__name__)


class HandlerDodVerify:
    """Handler for DoD evidence verification.

    When ``evidence_results`` are provided, behaves as pure logic (no I/O).
    When ``evidence_results`` is None, loads the ticket contract and runs
    evidence checks via EvidenceCollector.
    """

    def handle(
        self,
        payload: ModelDodVerifyStartCommand | dict[str, object],
        *,
        evidence_results: list[ModelEvidenceCheckResult] | None = None,
    ) -> ModelDodVerifyState | dict[str, object]:
        """Run DoD evidence verification and return final state.

        Supports two calling conventions:
        - Typed: handle(ModelDodVerifyStartCommand, ...) -> ModelDodVerifyState
        - RuntimeLocal shim: handle(dict) -> dict  (required by RuntimeLocal contract)

        OMN-13253: the first parameter is named ``payload`` so the RuntimeLocal
        adapter's single-parameter dispatch passes the validated command/dict
        positionally instead of keyword-fanning the model fields, and
        ``evidence_results`` is keyword-only so the adapter sees exactly one
        positional parameter.
        """
        if isinstance(payload, dict):
            return self._handle_dict(payload)
        return self._handle_typed(payload, evidence_results)

    def _handle_dict(self, payload: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal shim — translates dict in/out to typed handle."""
        command = ModelDodVerifyStartCommand(**payload)
        state = self._handle_typed(command)
        return state.model_dump(mode="json")

    @staticmethod
    def _make_collector() -> EvidenceCollector:
        """Create an EvidenceCollector instance. Override in tests to mock."""
        from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
            EvidenceCollector,
        )

        return EvidenceCollector()

    def _handle_typed(
        self,
        command: ModelDodVerifyStartCommand,
        evidence_results: list[ModelEvidenceCheckResult] | None = None,
    ) -> ModelDodVerifyState:
        """Run DoD evidence verification and return final state.

        Canonical typed entry point. Accepts a start command and optional
        pre-collected evidence results. When evidence_results is None,
        loads the contract and collects evidence automatically.
        """
        occ_governance_ref: str | None = None
        occ_refresh_outcome: EnumOccRefRefreshOutcome | None = None
        occ_resolved_sha: str | None = None
        # OMN-17022: the first PR/repo lookup failure of this run, as a typed
        # cause. None on the caller-supplied ``evidence_results`` path, which
        # never performed a lookup at all.
        lookup_failure_cause: EnumDodVerifyUnresolvedCause | None = None
        lookup_failure_code: str | None = None
        if evidence_results is None:
            collector = self._make_collector()
            evidence_results = collector.collect(
                ticket_id=command.ticket_id,
                contract_path=command.contract_path,
            )
            # OMN-15454 AC2: provenance of the OCC ref actually read this run,
            # not merely the ref requested. None when collect() never
            # attempted OCC auto-resolution (an explicit contract_path).
            if collector.occ_refresh_outcome is not None:
                occ_governance_ref = collector.occ_governance_ref
                occ_refresh_outcome = collector.occ_refresh_outcome
                occ_resolved_sha = collector.occ_resolved_sha
            # OMN-17022: read the same way — typed provenance the collector
            # already holds, never a message string parsed back out.
            lookup_failure_cause = collector.lookup_failure_cause
            lookup_failure_code = collector.lookup_failure_code

        checks = evidence_results

        verified = sum(
            1 for r in checks if r.status == EnumEvidenceCheckStatus.VERIFIED
        )
        failed = sum(1 for r in checks if r.status == EnumEvidenceCheckStatus.FAILED)
        skipped = sum(1 for r in checks if r.status == EnumEvidenceCheckStatus.SKIPPED)
        # OMN-15382: a superseded item is neither executed nor a failure — it
        # is excluded from the failure count (and from "all skipped" below)
        # entirely; the superseding item's own checks carry the verdict.
        superseded = sum(
            1 for r in checks if r.status == EnumEvidenceCheckStatus.SUPERSEDED
        )
        # OMN-16788: skips that are credential-reachability facts, not
        # deliberate ones. These carry a typed ``unverifiable_cause`` and are
        # the reason the SKIPPED branch below had to grow a new arm: an
        # ORDINARY skip is intentionally non-blocking (OMN-16087's
        # non-merged assertion; a disabled live-PR check), so degrading an
        # unreadable check from FAILED to a plain SKIPPED would have turned
        # OMN-15715's fail-closed refusal into a fail-OPEN pass the moment
        # any sibling check verified. Counted separately so the failure count
        # stays clean (the check did not fail) while the verdict stays
        # blocked (the check was never proven).
        unverifiable = [r for r in checks if r.unverifiable_cause is not None]
        # OMN-15391: executed, exited 0, and its exit status cannot depend on
        # the product change — a bare ``gh pr view`` (green for every PR on
        # GitHub) or a ticket-independent foreign suite. It is provenance, and
        # provenance is not completion, so it is counted on its own axis and
        # never folded into ``verified``.
        non_probative = sum(
            1 for r in checks if r.status == EnumEvidenceCheckStatus.NON_PROBATIVE
        )
        # OMN-15911: how many of the passing checks actually executed the
        # claimed behavior. The orthogonal question to ``non_probative``:
        # that one asks whether a check's exit status CAN depend on the
        # product change, this one asks what a check that PASSED bound.
        #
        # Both conjuncts are load-bearing. VERIFIED alone counts a merge-state
        # read; BEHAVIOR alone counts a test run that FAILED. Only their
        # conjunction is a proof, and a consuming lane that flips a ticket Done
        # on this tally needs it to be >= 1 — a green run whose every check is
        # an asserted `gh pr view … | grep -q MERGED` is probative under
        # OMN-15391 and still a statement about GitHub, not about the system.
        behavior_proving = sum(
            1
            for r in checks
            if r.status == EnumEvidenceCheckStatus.VERIFIED
            and r.proof_class == EnumCheckProofClass.BEHAVIOR
        )

        # OMN-15390: ``total_checks`` is the VERDICT-BEARING denominator, so a
        # fully-repaired contract reads N/N rather than N/(N+superseded). A
        # superseded entry was not executed and cannot pass, so leaving it in
        # the denominator makes a correctly-repaired contract permanently
        # report a shortfall (OMN-15192 reads 12/12, not 12/14) — the exact
        # signal an operator uses to decide whether a ticket is closeable.
        # The superseded entries stay in ``checks`` (and in ``superseded_count``)
        # so the receipt still shows the repair rather than hiding it.
        non_superseded_total = len(checks) - superseded
        unresolved_cause: EnumDodVerifyUnresolvedCause | None = None
        if lookup_failure_cause is not None and verified == 0:
            # OMN-17022 (off-rails A15). The run could not resolve the PR or
            # repo binding its checks are written against, and NOTHING verified.
            # Every check that needed the binding returned ``(False, "cannot
            # resolve …")``, which the collector renders as a FAILED check — so
            # the run reported a substantive red for evidence it never looked
            # at. That is exactly how OMN-14993 was recorded: ``failed``, with
            # ``PR_LOOKUP_FAILED`` buried in a message, when three merged PRs
            # existed the whole time. It is a tooling defect, not missing work.
            #
            # UNRESOLVED is not a relaxation — it blocks a Done-flip on the same
            # terms FAILED does, and ``_build_receipt`` still writes FAIL and the
            # CLI still exits 1. What changes is that the outcome is now typed,
            # so reconciliation can refuse to retry it (a retry reproduces a
            # binding defect exactly) instead of spending the backoff budget.
            #
            # ``verified == 0`` is the guard that keeps this narrow: if any
            # check DID prove something, a red alongside it is a real red and
            # the FAILED arm below keeps it.
            overall = EnumDodVerifyStatus.UNRESOLVED
            unresolved_cause = lookup_failure_cause
        elif failed > 0:
            overall = EnumDodVerifyStatus.FAILED
        elif verified == 0 and non_probative > 0:
            # OMN-15391 — the refusal, and the reason it is SKIPPED rather than
            # FAILED. Nothing went wrong: every check ran and exited 0. What is
            # missing is a check whose exit status could have gone the other
            # way for a product reason, so the run has no evidence to report,
            # not a red to report. SKIPPED is the gap-comment lane — the CLI
            # still exits 1 and ``_build_receipt`` still writes ``FAIL``, so a
            # Done flip is refused either way; the distinction is what an
            # operator is told to do about it.
            #
            # Ordered ahead of the supersession backstop below on purpose: a
            # supersession whose carrier turned out to be provenance lands here
            # with the specific diagnosis rather than the generic one. Both are
            # non-flip, so the ordering trades no strictness for legibility.
            overall = EnumDodVerifyStatus.SKIPPED
        elif superseded > 0 and verified == 0:
            # OMN-15390 anti-laundering BACKSTOP, and the reason it is FAILED
            # rather than SKIPPED: supersession may remove a FALSE red, never
            # manufacture a green.
            #
            # This is NOT the primary guard, and must not be mistaken for one:
            # a GLOBAL ``verified == 0`` is defeated by any single unrelated
            # passing sibling, so on its own it only caught the degenerate
            # all-superseded contract. The real rule is per-edge and lives in
            # the collector — ``EvidenceCollector._supersession_is_in_effect``
            # retires a target only when the superseding item is itself
            # VERIFIED, so a ``checks: []`` / skipping / failing marker item
            # retires nothing and its target executes normally.
            #
            # That makes this branch unreachable via the collector path
            # (SUPERSEDED implies a VERIFIED carrier implies ``verified > 0``;
            # asserted by ``test_a_superseded_entry_always_implies_a_verified
            # _carrier_across_the_whole_domain``). It is retained because
            # ``_handle_typed`` also accepts caller-supplied
            # ``evidence_results``, and that path has no such invariant — it
            # fails closed here rather than receipting a PASS built purely out
            # of supersessions.
            overall = EnumDodVerifyStatus.FAILED
        elif unverifiable:
            # OMN-16788: at least one check could not be EVALUATED — the
            # verifying credential was not permitted to read its evidence.
            # Not FAILED (nothing was found wanting) and emphatically not
            # VERIFIED (nothing was proven). This is the arm that preserves
            # OMN-15715 D1's fail-closed intent through the degrade: the
            # ticket cannot flip on evidence no one read.
            overall = EnumDodVerifyStatus.SKIPPED
        elif non_superseded_total == 0 or skipped == non_superseded_total:
            # Either nothing but superseded entries remain, or every
            # non-superseded check was skipped — do not claim VERIFIED.
            overall = EnumDodVerifyStatus.SKIPPED
        else:
            overall = EnumDodVerifyStatus.VERIFIED

        error_message: str | None = None
        if unresolved_cause is not None:
            # OMN-17022: a distinct, machine-checkable reason code, sitting
            # alongside CONTRACT_MISSING / NO_PROBATIVE_EVIDENCE /
            # EVIDENCE_UNVERIFIABLE. The remedy named here is a binding or a
            # credential — never "re-run it", which is what the untyped
            # RUN_ERROR_OR_TIMEOUT label invited for the whole held set.
            error_message = (
                f"VERIFICATION_UNRESOLVED: {unresolved_cause.value} — "
                f"0/{non_superseded_total} evidence checks for "
                f"{command.ticket_id} could be evaluated because the PR/repo "
                f"binding could not be resolved ({lookup_failure_code}). This "
                "is a resolution defect, not a verdict about the work: a retry "
                "reproduces it exactly. Bind REPO/PR_NUMBER, or name the "
                "owner/repo in the evidence item id per the autobind naming "
                "convention."
            )
        elif overall == EnumDodVerifyStatus.SKIPPED:
            # OMN-15380: surface a distinct, machine-checkable reason so callers
            # that only read ``error_message`` (e.g. RuntimeLocal._classify_result,
            # which treats a populated error_message as an unambiguous failure
            # signal) fail closed instead of silently succeeding on zero verified
            # checks. Missing/unresolvable contract gets its own reason code
            # because it is the highest-risk case: a ticket with no DoD contract
            # at all is exactly the one most likely to be a false-Done.
            if (
                len(checks) == 1
                and checks[0].evidence_id == "contract"
                and checks[0].status == EnumEvidenceCheckStatus.SKIPPED
            ):
                error_message = (
                    f"CONTRACT_MISSING: no DoD contract found for "
                    f"{command.ticket_id}; zero checks were verified"
                )
            elif verified == 0 and non_probative > 0:
                # OMN-15391: its own reason code, because the remedy is
                # specific and different from every other SKIP. The contract
                # is not missing and nothing was skipped — it declares checks
                # that all passed and none of which could have failed for a
                # product reason. The fix is to BIND a probative check, not to
                # re-run anything.
                error_message = (
                    f"NO_PROBATIVE_EVIDENCE: {non_probative}/{len(checks)} "
                    f"evidence checks for {command.ticket_id} executed and "
                    "passed, but every one of them is exit-status-invariant "
                    "over the product change (PR-existence probes, or a "
                    "ticket-independent foreign suite) — they are provenance, "
                    "not proof, and none of them counts toward completion. "
                    "Zero checks proved anything about this ticket. Bind a "
                    "check whose exit status depends on the product change "
                    "(a content read at a pinned ref, or a test this ticket's "
                    "diff makes pass) before claiming completion."
                )
            elif unverifiable:
                # OMN-16788: distinct reason code so a caller (and a human
                # reading the receipt) can tell "we were not permitted to read
                # this" apart from "nothing verified". NO_CHECKS_VERIFIED
                # would additionally be a lie here whenever a sibling check did
                # verify. The causes are named so the remedy — a scope grant,
                # or adding a repo to the App installation — is legible without
                # re-running the sweep under instrumentation.
                #
                # Positioned to MIRROR the verdict precedence above: the
                # OMN-15391 non-probative arm is evaluated first there, so its
                # message must be reachable first here, or a run decided by
                # that arm would be reported under this one's reason code.
                causes = sorted(
                    {
                        r.unverifiable_cause.value
                        for r in unverifiable
                        if r.unverifiable_cause is not None
                    }
                )
                error_message = (
                    f"EVIDENCE_UNVERIFIABLE: {len(unverifiable)}/"
                    f"{non_superseded_total} evidence check(s) for "
                    f"{command.ticket_id} could not be evaluated "
                    f"({', '.join(causes)}); {verified} verified, {failed} "
                    f"failed. An unread check is not a passed check — no "
                    f"Done-flip on evidence the verifier could not reach."
                )
            else:
                error_message = (
                    f"NO_CHECKS_VERIFIED: 0/{len(checks)} evidence checks "
                    f"verified for {command.ticket_id}"
                )

        state = ModelDodVerifyState(
            correlation_id=command.correlation_id,
            ticket_id=command.ticket_id,
            status=overall,
            dry_run=command.dry_run,
            checks=checks,
            total_checks=non_superseded_total,
            verified_count=verified,
            failed_count=failed,
            skipped_count=skipped,
            error_message=error_message,
            superseded_count=superseded,
            non_probative_count=non_probative,
            behavior_proving_count=behavior_proving,
            occ_governance_ref=occ_governance_ref,
            occ_refresh_outcome=occ_refresh_outcome,
            occ_resolved_sha=occ_resolved_sha,
            unresolved_cause=unresolved_cause,
        )

        return state

    def run_verification(
        self,
        command: ModelDodVerifyStartCommand,
        evidence_results: list[ModelEvidenceCheckResult] | None = None,
    ) -> tuple[ModelDodVerifyState, ModelDodVerifyCompletedEvent]:
        """Run a complete verification and return state + completion event.

        Convenience wrapper used by tests and event-bus consumers that need
        the completed event alongside the state.
        """
        started_at = datetime.now(tz=UTC)
        state = self._handle_typed(command, evidence_results)
        completed = self.make_completed_event(state, started_at)
        return state, completed

    def make_completed_event(
        self,
        state: ModelDodVerifyState,
        started_at: datetime,
    ) -> ModelDodVerifyCompletedEvent:
        """Create a completion event from the final state."""
        return ModelDodVerifyCompletedEvent(
            correlation_id=state.correlation_id,
            ticket_id=state.ticket_id,
            status=state.status,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            checks=state.checks,
            total_checks=state.total_checks,
            verified_count=state.verified_count,
            failed_count=state.failed_count,
            skipped_count=state.skipped_count,
            superseded_count=state.superseded_count,
            non_probative_count=state.non_probative_count,
            behavior_proving_count=state.behavior_proving_count,
            error_message=state.error_message,
            unresolved_cause=state.unresolved_cause,
        )

    def serialize_completed(self, event: ModelDodVerifyCompletedEvent) -> bytes:
        """Serialize a completed event to bytes."""
        return json.dumps(event.model_dump(mode="json")).encode()


__all__: list[str] = ["HandlerDodVerify"]
