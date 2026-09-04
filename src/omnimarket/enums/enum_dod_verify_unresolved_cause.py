# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Why a DoD verification reached no verdict at all (OMN-17022 / off-rails A15).

``EnumDodVerifyStatus`` was ``PENDING | VERIFIED | FAILED | SKIPPED``, and
``ModelDodVerifyState.status`` defaults to ``PENDING`` — so a run killed by a
caller-side timeout was indistinguishable from a run that never started. That
is why the ten items held in the 2026-08-29 sprint-triage closeout were never
re-run without a full-audit rerun: nothing in the record said which of them had
been attempted.

The ten are not ten of a kind. Nine carried ``RUN_ERROR_OR_TIMEOUT``, which was
**not a code symbol at all** — it was a label the ad-hoc batch runner invented
and it exists only in the closeout doc and the ledger. OMN-14993 carried
``PR_LOOKUP_FAILED``, an already-typed error code emitted by
``handler_dod_evidence_github_effect`` (``:467``, ``:494``, ``:507``). The two
need opposite policies: a transient run fault is worth another bounded attempt,
while a credential/resolution defect is reproduced exactly by retrying, so
retrying it burns the budget and reports the same thing.

Retry eligibility is therefore encoded on the member itself — the same shape
``EnumDispatchTerminalReason.auto_redispatchable`` uses one layer up (OMN-17018)
— so a caller that forgets to branch cannot retry a defect, and an
unclassifiable cause cannot become a retry by default.
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumDodVerifyUnresolvedCause(StrEnum):
    """Why an item is UNRESOLVED rather than verified, failed or skipped."""

    #: The verification run itself faulted or was killed — a caller-side
    #: timeout, a crashed process, an unhandled error in the sweep. The
    #: typed form of the ``RUN_ERROR_OR_TIMEOUT`` label the ad-hoc batch
    #: runner invented. Nothing was learned about the product, and the
    #: fault is not a property of the evidence, so another bounded attempt
    #: can legitimately reach a different answer.
    RUN_ERROR_OR_TIMEOUT = "run_error_or_timeout"
    #: OMN-17796. The OCC **governance ref** could not be materialised as a
    #: worktree — ``git worktree add --detach origin/dev`` failed, or blew the
    #: ``DOD_VERIFY_GIT_OP_TIMEOUT_S`` ceiling under contention on the single
    #: shared clone. Distinct in KIND from every cause above it: those are
    #: per-item, discovered while checks run; this one is resolved BEFORE the
    #: contract is loaded, so when it fires no evidence item has been read at
    #: all and nothing in the run is attributable to the ref the run reports.
    #: Named to match the code the collector already emits, so
    #: :meth:`from_error_code` resolves it with no second mapping table.
    OCC_WORKTREE_UNAVAILABLE = "occ_worktree_unavailable"
    #: OMN-17796, the twin one branch earlier: the ``git fetch`` of the
    #: governance ref itself failed (OMN-15454's refusal), so the local clone's
    #: freshness is UNKNOWN. Same scope, same consequence, same encoding —
    #: leaving it on the old arm would leave an identical armed path.
    OCC_REF_REFRESH_FAILED = "occ_ref_refresh_failed"
    #: ``gh`` could not resolve the ticket's PR (no ``REPO`` binding, an
    #: unreadable repo, an unauthenticated credential). OMN-14993's class.
    #: Deterministic in the verifier's configuration, not in the network:
    #: a retry reproduces it exactly. Remedy is a binding or a credential,
    #: never another attempt.
    PR_LOOKUP_FAILED = "pr_lookup_failed"
    #: More than one PR matched the ticket token, so no single PR could be
    #: bound. Also deterministic — the ambiguity does not decay with time.
    PR_LOOKUP_AMBIGUOUS = "pr_lookup_ambiguous"
    #: The owner/repo for the ticket could not be resolved at all. Same
    #: class as PR_LOOKUP_FAILED, one step earlier in the chain.
    REPO_LOOKUP_FAILED = "repo_lookup_failed"
    #: The cause could not be classified. Escalates to a human. It must
    #: never default to "retry" and must never read as healthy — an
    #: unclassified unresolved item is the state this taxonomy exists to
    #: stop laundering back into PENDING.
    UNKNOWN = "unknown"

    @property
    def retry_eligible(self) -> bool:
        """Whether a bounded retry may be scheduled for this cause.

        Encoded on the member so a caller that forgot to branch cannot retry
        a credential/resolution defect. Only the classes whose fault lives in
        the RUN — the verifier's own process or host — are eligible; every
        binding/credential class reproduces exactly and stays refused.

        OMN-17796 added the two OCC governance-ref causes on that same test.
        They are load- and lock-dependent facts about the machine (the trip
        measured 2026-09-03 was a 300 s ``git worktree add`` ceiling under
        6-way parallelism on one shared clone), and the collector has already
        spent its one in-run retry by the time the cause is set — so the next
        attempt has to be a scheduled one, which is exactly what eligibility
        authorises. Eligible does not mean "will succeed": it means another
        bounded attempt can legitimately reach a different answer.
        """
        return self in _RETRY_ELIGIBLE_CAUSES

    @classmethod
    def from_error_code(cls, error_code: str) -> EnumDodVerifyUnresolvedCause:
        """Map an already-typed github-effect error code onto this taxonomy.

        Accepts either a bare code (``"PR_LOOKUP_FAILED"``) or the
        ``"CODE: detail"`` rendering that ``EvidenceCollector`` stores in
        ``_last_pr_lookup_error``, so no producer has to be changed to strip
        its own detail before reporting.

        An unrecognised code resolves to :attr:`UNKNOWN`, which is NOT
        retry-eligible. That is the fail-closed direction: a code this
        taxonomy has not seen must not inherit the retry policy of one it
        has. The original code string is preserved verbatim on the attempt
        record, so nothing is lost by the mapping.
        """
        head = error_code.split(":", 1)[0].strip().upper()
        try:
            return cls(head.lower())
        except ValueError:
            return cls.UNKNOWN


#: The single place eligibility is declared. Membership is opt-IN, so a member
#: added later without a considered retry policy is non-retryable by default —
#: the fail-closed direction, matching :meth:`from_error_code`'s treatment of
#: an unrecognised code.
_RETRY_ELIGIBLE_CAUSES: frozenset[EnumDodVerifyUnresolvedCause] = frozenset(
    {
        EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
        EnumDodVerifyUnresolvedCause.OCC_WORKTREE_UNAVAILABLE,
        EnumDodVerifyUnresolvedCause.OCC_REF_REFRESH_FAILED,
    }
)

#: Causes for which a bounded retry is refused outright. Derived from
#: :attr:`EnumDodVerifyUnresolvedCause.retry_eligible` so the two can never
#: disagree.
NON_RETRYABLE_CAUSES: frozenset[EnumDodVerifyUnresolvedCause] = frozenset(
    cause for cause in EnumDodVerifyUnresolvedCause if not cause.retry_eligible
)


__all__: list[str] = ["NON_RETRYABLE_CAUSES", "EnumDodVerifyUnresolvedCause"]
