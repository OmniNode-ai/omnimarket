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
from omnimarket.enums.enum_dod_acceptance_basis import EnumDodAcceptanceBasis
from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_acceptance_summary import (
    ModelDodAcceptanceSummary,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_contract_subject import (
    ModelDodContractSubject,
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
from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumDurableEvidenceCheck,
    ModelDurableEvidenceGateRun,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    _ALLOW_STALE_OCC_REF_ENV,
    _GIT_OP_TIMEOUT_ENV,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
        EvidenceCollector,
    )

logger = logging.getLogger(__name__)

# OMN-20886: the gate checks the dod_verify skill names as its pre-Done checks.
# The gate's released, repair-to-ratchet and done-class checks read the
# ticket's labels and decide the Linear transition itself; they are evaluated
# but are not checks of this verdict.
DURABLE_GATE_VERDICT_CHECKS: tuple[EnumDurableEvidenceCheck, ...] = (
    EnumDurableEvidenceCheck.RECEIPT_TRACKED,
    EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT,
    EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN,
)


def durable_gate_check_results(
    gate_run: ModelDurableEvidenceGateRun,
) -> list[ModelEvidenceCheckResult]:
    """The gate's verdict checks as evidence results, in the gate's order."""
    results: list[ModelEvidenceCheckResult] = []
    for check in gate_run.result.checks:
        if check.check not in DURABLE_GATE_VERDICT_CHECKS:
            continue
        message = check.message
        if not check.passed and gate_run.ticket_read_detail:
            message = f"{message} (ticket unreadable: {gate_run.ticket_read_detail})"
        results.append(
            ModelEvidenceCheckResult(
                evidence_id=f"durable_gate::{check.check.value}",
                description=f"DurableEvidenceGate {check.check.value}",
                status=(
                    EnumEvidenceCheckStatus.VERIFIED
                    if check.passed
                    else EnumEvidenceCheckStatus.FAILED
                ),
                message=message,
            )
        )
    return results


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

        OMN-18901: the returned state carries the run window, because the
        runtime publishes this returned model on the node's declared terminal
        topic and the projection consuming it requires both timestamps. Read
        before any evidence is collected, so the window spans the actual work
        rather than starting after the slowest part of it.
        """
        started_at = datetime.now(tz=UTC)
        occ_governance_ref: str | None = None
        occ_refresh_outcome: EnumOccRefRefreshOutcome | None = None
        occ_resolved_sha: str | None = None
        contract_subject: ModelDodContractSubject | None = None
        # OMN-17022: the first PR/repo lookup failure of this run, as a typed
        # cause. None on the caller-supplied ``evidence_results`` path, which
        # never performed a lookup at all.
        lookup_failure_cause: EnumDodVerifyUnresolvedCause | None = None
        lookup_failure_code: str | None = None
        # OMN-17796: the run-wide sibling of the two above. Set when collect()
        # refused on the OCC governance ref itself, which happens BEFORE the
        # contract is loaded.
        occ_ref_failure_cause: EnumDodVerifyUnresolvedCause | None = None
        occ_ref_failure_code: str | None = None
        # OMN-20153: what the collector derived from the ticket contract's
        # accepted falsifiers. Stays None on the caller-supplied
        # ``evidence_results`` path and on a goal-scoped run, neither of which
        # loads a ticket contract, so "not measured" is never "no checks".
        acceptance_summary: ModelDodAcceptanceSummary | None = None
        execution_audience = command.execution_audience
        if execution_audience is None and evidence_results is None:
            evidence_results = [
                ModelEvidenceCheckResult(
                    evidence_id="execution_audience",
                    description="Authorized DoD evidence execution audience",
                    status=EnumEvidenceCheckStatus.FAILED,
                    message=(
                        "EXECUTION_AUDIENCE_REQUIRED: node_dod_verify refused "
                        "before loading the contract or executing any evidence. "
                        "Set execution_audience to 'hosted' or "
                        "'local_done_gate'."
                    ),
                )
            ]
        if evidence_results is None:
            assert execution_audience is not None
            collector = self._make_collector()
            if command.goal_id is not None:
                assert command.contract_schema_version is not None
                evidence_results = collector.collect_inline(
                    ticket_id=command.ticket_id,
                    dod_evidence=command.dod_evidence,
                    goal_id=command.goal_id,
                    contract_schema_version=command.contract_schema_version,
                    execution_audience=execution_audience,
                )
            else:
                evidence_results = collector.collect(
                    ticket_id=command.ticket_id,
                    contract_path=command.contract_path,
                    execution_audience=execution_audience,
                )
            # OMN-15454 AC2: provenance of the OCC ref actually read this run,
            # not merely the ref requested. None when collect() never
            # attempted OCC auto-resolution (an explicit contract_path).
            if collector.occ_refresh_outcome is not None:
                occ_governance_ref = collector.occ_governance_ref
                occ_refresh_outcome = collector.occ_refresh_outcome
                occ_resolved_sha = collector.occ_resolved_sha
            # ``getattr`` because ``_make_collector`` is the documented seam a
            # dozen suites replace with a stub; a stub that never derived
            # anything reads as "not measured", never as "no checks".
            acceptance_summary = getattr(collector, "acceptance_summary", None)
            contract_subject = getattr(collector, "contract_subject", None)
            # OMN-17022: read the same way — typed provenance the collector
            # already holds, never a message string parsed back out.
            lookup_failure_cause = collector.lookup_failure_cause
            lookup_failure_code = collector.lookup_failure_code
            # OMN-17796: read the same way, for the same reason.
            occ_ref_failure_cause = collector.occ_ref_failure_cause
            occ_ref_failure_code = collector.occ_ref_failure_code
            # OMN-20886: the DurableEvidenceGate runs on the auto-resolved
            # path, so its three pre-Done checks are checks of this verdict.
            # ``getattr`` for the same stub seam as above: a stub that never
            # resolved a contract runs no gate.
            run_durable_gate = getattr(collector, "run_durable_gate", None)
            gate_run = run_durable_gate() if callable(run_durable_gate) else None
            if isinstance(gate_run, ModelDurableEvidenceGateRun):
                evidence_results = [
                    *evidence_results,
                    *durable_gate_check_results(gate_run),
                ]

        checks = evidence_results
        executable_checks = [r for r in checks if not r.is_disposition]

        verified = sum(
            1 for r in executable_checks if r.status == EnumEvidenceCheckStatus.VERIFIED
        )
        failed = sum(
            1 for r in executable_checks if r.status == EnumEvidenceCheckStatus.FAILED
        )
        skipped = sum(
            1 for r in executable_checks if r.status == EnumEvidenceCheckStatus.SKIPPED
        )
        # OMN-15382: a superseded item is neither executed nor a failure — it
        # is excluded from the failure count (and from "all skipped" below)
        # entirely; the superseding item's own checks carry the verdict.
        superseded = sum(
            1
            for r in executable_checks
            if r.status == EnumEvidenceCheckStatus.SUPERSEDED
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
        unverifiable = [
            r for r in executable_checks if r.unverifiable_cause is not None
        ]
        # OMN-15391: executed, exited 0, and its exit status cannot depend on
        # the product change — a bare ``gh pr view`` (green for every PR on
        # GitHub) or a ticket-independent foreign suite. It is provenance, and
        # provenance is not completion, so it is counted on its own axis and
        # never folded into ``verified``.
        non_probative = sum(
            1
            for r in executable_checks
            if r.status == EnumEvidenceCheckStatus.NON_PROBATIVE
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
            for r in executable_checks
            if r.status == EnumEvidenceCheckStatus.VERIFIED
            and r.proof_class == EnumCheckProofClass.BEHAVIOR
        )
        # OMN-18135 AC4: counted alongside, never added into, the line above.
        readback_proving = sum(
            1
            for r in executable_checks
            if r.status == EnumEvidenceCheckStatus.VERIFIED
            and r.proof_class == EnumCheckProofClass.READBACK
        )

        # OMN-15390: ``total_checks`` is the VERDICT-BEARING denominator, so a
        # fully-repaired contract reads N/N rather than N/(N+superseded). A
        # superseded entry was not executed and cannot pass, so leaving it in
        # the denominator makes a correctly-repaired contract permanently
        # report a shortfall (OMN-15192 reads 12/12, not 12/14) — the exact
        # signal an operator uses to decide whether a ticket is closeable.
        # The superseded entries stay in ``checks`` (and in ``superseded_count``)
        # so the receipt still shows the repair rather than hiding it.
        # OMN-17323: the SECOND class of entry that carries no product-dependent
        # verdict, excluded on exactly the reasoning above. An unbindable
        # ``::pr-live-state`` overlay is not a criterion the TICKET declared —
        # it is one the VERIFIER auto-derives for every evidence item, and this
        # one skipped because the verifier's own binder derived no (repo, pr)
        # pair. It was never executed and can never pass, so counting it in the
        # denominator reports the tool's inability as the ticket's shortfall.
        #
        # This is load-bearing, not cosmetic: OMN-16434 auto-mints
        # ``dod-occ-diff-derived-behavior-proof`` onto every new OCC companion,
        # so every freshly-minted companion carried exactly one guaranteed
        # unbindable overlay and the OMN-16821 autoclose flip equality
        # (``verified + non_probative == total``) was unsatisfiable BY
        # CONSTRUCTION for the whole corpus — 24 consecutive scheduled runs,
        # ~660 companion scans, FLIP=0 in every one.
        #
        # Deliberately narrow. The three other SKIPPED shapes — a
        # live-PR-check-disabled skip, an OMN-16087 intentional non-merged
        # assertion, and an OMN-16788 ``unverifiable_cause`` skip — carry no
        # marker, stay in the denominator, and keep blocking on their existing
        # terms.
        unbindable_overlays = sum(
            1 for r in executable_checks if r.unbindable_derived_overlay
        )
        non_superseded_total = len(executable_checks) - superseded - unbindable_overlays
        # The marker is only valid on a SKIPPED result (enforced on the model),
        # so every excluded overlay is also inside ``skipped``. Both sides of
        # the "everything was skipped" comparison below must therefore drop
        # them, or a single genuine skip alongside one overlay stops matching
        # and falls through to VERIFIED with zero verified checks — a fail-OPEN
        # introduced by the exclusion itself. ``skipped_count`` on the state
        # keeps the raw tally; only this comparison uses the narrowed one.
        verdict_bearing_skipped = skipped - unbindable_overlays
        unresolved_cause: EnumDodVerifyUnresolvedCause | None = None
        if occ_ref_failure_cause is not None:
            # OMN-17796. The OCC governance ref — the thing every contract in
            # this corpus is READ FROM — could not be resolved, so ``collect()``
            # returned before loading a contract at all. The old encoding sent
            # this down the ``elif failed > 0`` arm and emitted
            # ``FAILED / total_checks=1 / failed_count=1`` with no
            # ``error_message``: a substantive red about a ticket the run never
            # opened. Measured 2026-09-03, 12 of 36 runs, one identical
            # ``verdict_content_sha256`` across six unrelated tickets.
            #
            # Deliberately NOT guarded by ``verified == 0``, unlike the
            # per-item arm below. That guard exists so a binding failure
            # discovered mid-run cannot steal a real red that ran beside it —
            # a per-item concern. This cause is run-wide and antecedent: if the
            # governance ref did not resolve, nothing produced under it is
            # attributable to the ref the receipt names, INCLUDING a check that
            # happened to pass. Firing unconditionally is therefore strictly
            # stricter than the arm it replaces, not a relaxation of it.
            overall = EnumDodVerifyStatus.UNRESOLVED
            unresolved_cause = occ_ref_failure_cause
        elif lookup_failure_cause is not None and verified == 0:
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
        elif (
            command.goal_id is not None
            and not executable_checks
            and any(result.is_disposition for result in checks)
        ):
            # A disposition is durable caller evidence, not an executable
            # verifier check. Report the run as collected so the shared core
            # reducer reaches NO_CHECKS_RUN over the empty executable set.
            overall = EnumDodVerifyStatus.VERIFIED
        elif (
            non_superseded_total == 0 or verdict_bearing_skipped == non_superseded_total
        ):
            # Either no verdict-bearing entry remains at all (only superseded
            # entries and/or OMN-17323 unbindable overlays), or every one that
            # does was skipped — do not claim VERIFIED. The first disjunct is
            # what keeps the OMN-17323 exclusion from manufacturing a green out
            # of a contract whose only entries are unbindable overlays.
            overall = EnumDodVerifyStatus.SKIPPED
        else:
            overall = EnumDodVerifyStatus.VERIFIED

        # OMN-20153: the DoD verdict requires the author's own falsifier checks.
        # Two refusals, both only ever DOWNGRADING a VERIFIED verdict, never
        # upgrading anything:
        #
        # * a derived falsifier item that is not VERIFIED (a plain skip is
        #   otherwise non-blocking) means a criterion the author declared was
        #   not proven, so the run is SKIPPED, not VERIFIED;
        # * a contract with no runnable falsifier that reads VERIFIED with every
        #   passing check a PR-state probe or a readback
        #   passed on "the change landed" alone, so it is SKIPPED with
        #   NO_ACCEPTANCE_CHECKS rather than reported as proven. A ticket
        #   carrying a behavior-proving check, or a check of a kind the
        #   classifier cannot place, keeps its verdict and only gains the basis
        #   field that says what it rests on.
        acceptance_basis: EnumDodAcceptanceBasis | None = (
            acceptance_summary.basis if acceptance_summary is not None else None
        )
        unproven_falsifier_ids: list[str] = []
        no_acceptance_demotion = False
        if acceptance_summary is not None and overall == EnumDodVerifyStatus.VERIFIED:
            verified_ids = {
                r.evidence_id
                for r in executable_checks
                if r.status == EnumEvidenceCheckStatus.VERIFIED
            }
            # Provenance-or-readback only: every check that verified is a
            # PR-state probe or a content read at a pinned ref (the
            # machine-made evidence the audit counted). None of them
            # executes the claimed behavior (OMN-18135: a readback never proves
            # behavior), and with no author-declared falsifier there is nothing
            # to say the readback answers a live-state criterion. A check of an
            # unclassifiable kind is left alone, so a hand-authored contract
            # keeps the verdict it always had.
            provenance_only = all(
                r.proof_class
                in (
                    EnumCheckProofClass.MERGE_STATE,
                    EnumCheckProofClass.READBACK,
                )
                for r in executable_checks
                if r.status == EnumEvidenceCheckStatus.VERIFIED
            )
            unproven_falsifier_ids = [
                item_id
                for item_id in acceptance_summary.derived_item_ids
                if item_id not in verified_ids
            ]
            if unproven_falsifier_ids:
                overall = EnumDodVerifyStatus.SKIPPED
            elif (
                acceptance_basis is not EnumDodAcceptanceBasis.FALSIFIER_CHECKS
                and behavior_proving == 0
                and provenance_only
            ):
                overall = EnumDodVerifyStatus.SKIPPED
                no_acceptance_demotion = True

        # OMN-17427: a binding's author cannot accept it, even with passing checks.
        self_acceptance_demotion = False
        if (
            acceptance_summary is not None
            and acceptance_summary.self_accepted_bindings
            and overall == EnumDodVerifyStatus.VERIFIED
        ):
            overall = EnumDodVerifyStatus.SKIPPED
            self_acceptance_demotion = True

        # OMN-20070: a repo-owned contract binds every declared criterion, so
        # a criterion no item binds is unproven. This rule used to live only
        # in omnimarket's own contract-binds test.
        unbound_demotion = False
        if (
            acceptance_summary is not None
            and acceptance_summary.unbound_criteria
            and overall == EnumDodVerifyStatus.VERIFIED
        ):
            overall = EnumDodVerifyStatus.SKIPPED
            unbound_demotion = True

        error_message: str | None = None
        if occ_ref_failure_cause is not None:
            # OMN-17796: its own remedy text, because OMN-17022's below is the
            # WRONG instruction for this cause on both counts — it names the
            # PR/repo binding, and it states that "a retry reproduces it
            # exactly", which is true of a credential defect and false of a
            # contention-driven git ceiling trip, where another attempt on a
            # quieter host is precisely the right move.
            error_message = (
                f"VERIFICATION_UNRESOLVED: {occ_ref_failure_cause.value} — the "
                f"OCC governance ref {occ_governance_ref} could not be "
                f"resolved ({occ_ref_failure_code}), so no contract was loaded "
                f"and 0 evidence checks for {command.ticket_id} were "
                "evaluated. This is a fact about the verifier's host, not a "
                "verdict about the ticket: the same refusal is produced for "
                "every ticket on a host in this state. Remedy is to re-run "
                "when the OCC clone is not under contention, or to raise the "
                f"git-op ceiling with {_GIT_OP_TIMEOUT_ENV}; "
                f"{_ALLOW_STALE_OCC_REF_ENV}=1 proceeds against the "
                "main-tracking working tree instead, and marks every result "
                "un-attributable."
            )
        elif unresolved_cause is not None:
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
        elif unproven_falsifier_ids:
            error_message = (
                f"AC_FALSIFIER_NOT_VERIFIED: {len(unproven_falsifier_ids)} of "
                f"{len(acceptance_summary.derived_item_ids) if acceptance_summary else 0} "
                f"acceptance-criterion falsifier check(s) for {command.ticket_id} "
                f"did not verify ({', '.join(unproven_falsifier_ids)}). The "
                "author declared these checks before the work existed; a "
                "criterion whose falsifier was not run is not proven."
            )
        elif self_acceptance_demotion:
            bindings = (
                acceptance_summary.self_accepted_bindings if acceptance_summary else ()
            )
            error_message = (
                f"AC_BINDING_SELF_ACCEPTED: {len(bindings)} acceptance-criterion "
                f"binding(s) for {command.ticket_id} were accepted by the lane "
                f"that authored them, or by no one ({', '.join(bindings)}). A binding is "
                "accepted by a second lane that re-runs the bound check, never "
                "by its author; until then the criterion is unproven."
            )
            if acceptance_summary is not None and acceptance_summary.retired_bindings:
                error_message += (
                    " Retired bindings (not counted): "
                    + "; ".join(acceptance_summary.retired_bindings)
                    + "."
                )
        elif unbound_demotion:
            unbound = (
                acceptance_summary.unbound_criteria
                if acceptance_summary is not None
                else ()
            )
            n = len(unbound)
            error_message = (
                f"NO_ACCEPTANCE_CHECKS: {command.ticket_id} declares {n} acceptance "
                f"{'criterion' if n == 1 else 'criteria'} that no dod_evidence item "
                f"binds through binds_ac ({', '.join(unbound)}). Bind each criterion "
                "to a check whose test fails without the change; an unbound "
                "criterion is unproven."
            )
        elif no_acceptance_demotion:
            declared = (
                acceptance_summary.declared_falsifier_count
                if acceptance_summary is not None
                else 0
            )
            error_message = (
                f"NO_ACCEPTANCE_CHECKS: {command.ticket_id} has "
                f"{'no accepted acceptance-criterion falsifier' if declared == 0 else f'{declared} accepted falsifier(s), none a runnable test selector'}"
                ", and no check in this run executed the claimed behavior. "
                "Every passing check is a PR-exists, grep or readback probe, "
                "which says the change landed and not that it does what the "
                "ticket asked. Write the ticket's criteria with falsifiers that "
                "name a test selector so they run as checks."
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
                    f"NO_PROBATIVE_EVIDENCE: {non_probative}/"
                    f"{len(executable_checks)} "
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
                    f"NO_CHECKS_VERIFIED: 0/{len(executable_checks)} evidence checks "
                    f"verified for {command.ticket_id}"
                )

        if overall == EnumDodVerifyStatus.FAILED and error_message is None:
            failures = [
                f"{check.evidence_id}: "
                + (
                    check.failure.summary()
                    if check.failure is not None
                    else (check.message or "check failed")
                )
                for check in executable_checks
                if check.status == EnumEvidenceCheckStatus.FAILED
            ]
            error_message = "EVIDENCE_CHECK_FAILED: " + " | ".join(failures)

        state = ModelDodVerifyState(
            correlation_id=command.correlation_id,
            ticket_id=command.ticket_id,
            status=overall,
            dry_run=command.dry_run,
            delegation_correlation_id=command.delegation_correlation_id,
            goal_id=command.goal_id,
            parent_goal_id=command.parent_goal_id,
            level=command.level,
            contract_revision=command.contract_revision,
            contract_source=(
                contract_subject.source if contract_subject is not None else None
            ),
            contract_repository=(
                contract_subject.repository if contract_subject is not None else None
            ),
            contract_commit_sha=(
                contract_subject.commit_sha if contract_subject is not None else None
            ),
            contract_repo_path=(
                contract_subject.repo_path if contract_subject is not None else None
            ),
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            checks=checks,
            total_checks=non_superseded_total,
            verified_count=verified,
            failed_count=failed,
            skipped_count=skipped,
            error_message=error_message,
            superseded_count=superseded,
            non_probative_count=non_probative,
            behavior_proving_count=behavior_proving,
            readback_proving_count=readback_proving,
            unbindable_overlay_count=unbindable_overlays,
            acceptance_basis=acceptance_basis,
            acceptance_declared_falsifier_count=(
                acceptance_summary.declared_falsifier_count if acceptance_summary else 0
            ),
            acceptance_runnable_falsifier_count=(
                acceptance_summary.runnable_count if acceptance_summary else 0
            ),
            acceptance_unrunnable_labels=(
                acceptance_summary.unrunnable_labels if acceptance_summary else ()
            ),
            acceptance_self_accepted_bindings=(
                acceptance_summary.self_accepted_bindings if acceptance_summary else ()
            ),
            acceptance_retired_bindings=(
                acceptance_summary.retired_bindings if acceptance_summary else ()
            ),
            acceptance_refused_retirements=(
                acceptance_summary.refused_retirements if acceptance_summary else ()
            ),
            acceptance_unbound_criteria=(
                acceptance_summary.unbound_criteria if acceptance_summary else ()
            ),
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

        OMN-18901: the run window comes off the state, which now owns it.
        This wrapper used to read the clock itself and hand its own
        ``started_at`` down, so the pair one verification produced disagreed
        about when that verification began.
        """
        state = self._handle_typed(command, evidence_results)
        completed = self.make_completed_event(state)
        return state, completed

    def make_completed_event(
        self,
        state: ModelDodVerifyState,
    ) -> ModelDodVerifyCompletedEvent:
        """Create a completion event from the final state.

        OMN-18901: the run window is read off the state rather than re-read
        from the clock, so the event and the state a single run produces
        cannot disagree about when that run happened. There is no parameter
        for a caller's own start time: a second source for the window is how
        the pair came to disagree in the first place.
        """
        return ModelDodVerifyCompletedEvent(
            correlation_id=state.correlation_id,
            ticket_id=state.ticket_id,
            status=state.status,
            delegation_correlation_id=state.delegation_correlation_id,
            goal_id=state.goal_id,
            parent_goal_id=state.parent_goal_id,
            level=state.level,
            contract_revision=state.contract_revision,
            contract_source=state.contract_source,
            contract_repository=state.contract_repository,
            contract_commit_sha=state.contract_commit_sha,
            contract_repo_path=state.contract_repo_path,
            started_at=state.started_at,
            completed_at=state.completed_at,
            checks=state.checks,
            total_checks=state.total_checks,
            verified_count=state.verified_count,
            failed_count=state.failed_count,
            skipped_count=state.skipped_count,
            superseded_count=state.superseded_count,
            non_probative_count=state.non_probative_count,
            behavior_proving_count=state.behavior_proving_count,
            readback_proving_count=state.readback_proving_count,
            unbindable_overlay_count=state.unbindable_overlay_count,
            acceptance_basis=state.acceptance_basis,
            acceptance_declared_falsifier_count=(
                state.acceptance_declared_falsifier_count
            ),
            acceptance_runnable_falsifier_count=(
                state.acceptance_runnable_falsifier_count
            ),
            acceptance_unrunnable_labels=state.acceptance_unrunnable_labels,
            error_message=state.error_message,
            unresolved_cause=state.unresolved_cause,
        )

    def serialize_completed(self, event: ModelDodVerifyCompletedEvent) -> bytes:
        """Serialize a completed event to bytes."""
        return json.dumps(event.model_dump(mode="json")).encode()


__all__: list[str] = ["HandlerDodVerify"]
