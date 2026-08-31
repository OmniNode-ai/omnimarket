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
        a credential/resolution defect. Only the run-fault class is eligible.
        """
        return self is EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT

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


#: Causes for which a bounded retry is refused outright. Derived from
#: :attr:`EnumDodVerifyUnresolvedCause.retry_eligible` so the two can never
#: disagree.
NON_RETRYABLE_CAUSES: frozenset[EnumDodVerifyUnresolvedCause] = frozenset(
    cause for cause in EnumDodVerifyUnresolvedCause if not cause.retry_eligible
)


__all__: list[str] = ["NON_RETRYABLE_CAUSES", "EnumDodVerifyUnresolvedCause"]
