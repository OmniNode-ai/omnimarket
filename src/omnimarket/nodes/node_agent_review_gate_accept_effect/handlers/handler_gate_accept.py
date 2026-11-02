# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide whether a verified agent receipt replaces the CI voters of one gate attempt.

``handle(request) -> result`` returns exactly one decision:

* ``unchanged`` -- the voters' verdict stands: the quorum passed, a CI ``blocked`` stands,
  a succeeded voter raised a blocking finding, an unsuccessful voter has no typed cause,
  or the attempt shows no receipt by the publisher;
* ``pass_agent`` -- the attempt is eligible (a degraded verdict, no blocking finding from
  a succeeded voter, a typed cause for every unsuccessful voter) and at least one receipt
  by the publisher verifies as accepted for the exact head;
* ``refuse`` -- anything that cannot be read or verified, or a receipt the verifier
  refused: the result file, the reviews, the verifier, a malformed receipt.

The rules are data (``ModelGateAcceptPolicy``) and the world is injected: the voter
result and the step files through ``files``, the reviews through ``reviews``, the
receipts' verdict through ``verifier``. The handler decides nothing the verifier did not
accept.
"""

from __future__ import annotations

import json
import re
from typing import Any

from omnimarket.nodes.node_agent_review_gate_accept_effect.adapters.adapter_gate_accept_ports import (
    LocalStepFiles,
    UnboundReceiptVerifier,
    UnboundReviewReader,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.models.model_gate_accept import (
    EnumGateAcceptDecision,
    EnumGateAcceptReason,
    EnumVoterCause,
    ModelGateAcceptPolicy,
    ModelGateAcceptRequest,
    ModelGateAcceptResult,
    ModelReceiptVerifyRequest,
    ModelVoterStatus,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.overlay import (
    load_gate_accept_policy,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.protocols.protocol_gate_accept import (
    ProtocolReceiptVerifier,
    ProtocolReviewReader,
    ProtocolStepFiles,
)

_PASSED = "passed"
_BLOCKED = "blocked"
_UNTYPED = "untyped"


class HandlerGateAccept:
    """Reads the attempt, asks the verifier about each receipt, writes the decision."""

    def __init__(
        self,
        *,
        policy: ModelGateAcceptPolicy | None = None,
        reviews: ProtocolReviewReader | None = None,
        verifier: ProtocolReceiptVerifier | None = None,
        files: ProtocolStepFiles | None = None,
    ) -> None:
        """Every port and the policy are injected; none is read at construction.

        A runtime builds the handler from its contract with no arguments, so the policy
        (the overlay's publisher identity and verifier endpoint) is read when the first
        request arrives, and a port left unbound fails closed. A missing overlay refuses
        the request, named, rather than guessing an identity.
        """
        self._given_policy = policy
        self._reviews = reviews if reviews is not None else UnboundReviewReader()
        self._verifier = verifier if verifier is not None else UnboundReceiptVerifier()
        self._files = files if files is not None else LocalStepFiles()
        self._bound: (
            tuple[
                ModelGateAcceptPolicy,
                re.Pattern[str],
                tuple[tuple[EnumVoterCause, tuple[re.Pattern[str], ...]], ...],
            ]
            | None
        ) = None

    def _bind(
        self,
    ) -> tuple[
        ModelGateAcceptPolicy,
        re.Pattern[str],
        tuple[tuple[EnumVoterCause, tuple[re.Pattern[str], ...]], ...],
    ]:
        if self._bound is None:
            policy = (
                self._given_policy
                if self._given_policy is not None
                else load_gate_accept_policy()
            )
            fence = re.compile(
                r"```"
                + re.escape(policy.receipt_fence)
                + r"[ \t]*\r?\n(?P<body>.*?)\r?\n```",
                re.DOTALL,
            )
            causes = (
                (
                    EnumVoterCause.CREDIT_EXHAUSTED,
                    tuple(re.compile(p) for p in policy.causes.credit_exhausted),
                ),
                (
                    EnumVoterCause.TIMEOUT,
                    tuple(re.compile(p) for p in policy.causes.timeout),
                ),
                (
                    EnumVoterCause.UNAVAILABLE,
                    tuple(re.compile(p) for p in policy.causes.unavailable),
                ),
            )
            self._bound = (policy, fence, causes)
        return self._bound

    def handle(self, request: ModelGateAcceptRequest) -> ModelGateAcceptResult:
        result = self._decide(request)
        self._files.append_text(
            request.step_output_path,
            f"decision={result.decision.value}\n"
            f"reason={result.reason.value}\n"
            f"receipt_id={result.receipt_id}\n",
        )
        self._files.append_text(request.step_summary_path, result.summary_line + "\n")
        return result

    # -- the attempt ---------------------------------------------------------

    def _decide(self, request: ModelGateAcceptRequest) -> ModelGateAcceptResult:
        try:
            self._bind()
        except (
            Exception
        ):  # no overlay or an invalid one: the verdict stands as produced
            return self._result(
                EnumGateAcceptDecision.REFUSE, EnumGateAcceptReason.POLICY_UNAVAILABLE
            )
        read = self._read_attempt(request.voter_result_path)
        if read is None:
            return self._result(
                EnumGateAcceptDecision.REFUSE,
                EnumGateAcceptReason.VOTER_RESULT_UNREADABLE,
            )
        verdict, voters = read
        if verdict == _PASSED:
            return self._result(
                EnumGateAcceptDecision.UNCHANGED,
                EnumGateAcceptReason.VOTERS_PASSED,
                voters=voters,
            )
        if verdict == _BLOCKED:
            return self._result(
                EnumGateAcceptDecision.UNCHANGED,
                EnumGateAcceptReason.CI_BLOCKED,
                voters=voters,
            )
        ineligible = self._ineligible(voters)
        if ineligible is not None:
            return self._result(
                EnumGateAcceptDecision.UNCHANGED, ineligible, voters=voters
            )
        return self._receipts(request, voters)

    def _read_attempt(self, path: str) -> tuple[str, list[ModelVoterStatus]] | None:
        try:
            document: Any = json.loads(self._files.read_text(path))
        except (OSError, ValueError):
            return None
        if not isinstance(document, dict):
            return None
        entries = document.get("results")
        if not isinstance(entries, list) or not entries:
            return None
        if not all(isinstance(entry, dict) for entry in entries):
            return None
        voters = [self._voter(entry) for entry in entries]
        quorum = document.get("quorum")
        verdict = ""
        if isinstance(quorum, dict):
            verdict = str(quorum.get("verdict", ""))
        if not verdict:
            succeeded = document.get("models_succeeded")
            verdict = _PASSED if succeeded else "degraded"
        return verdict, voters

    def _voter(self, entry: dict[str, Any]) -> ModelVoterStatus:
        succeeded = entry.get("success") is True
        findings = entry.get("findings")
        blocking = (
            succeeded
            and isinstance(findings, list)
            and any(
                isinstance(finding, dict)
                and str(finding.get("severity", "")).lower()
                in self._bind()[0].blocking_severities
                for finding in findings
            )
        )
        return ModelVoterStatus(
            voter=str(entry.get("model", "?")),
            succeeded=succeeded,
            blocking=blocking,
            cause=None if succeeded else self._cause(str(entry.get("error") or "")),
        )

    def _cause(self, error: str) -> EnumVoterCause | None:
        for cause, patterns in self._bind()[2]:
            if any(pattern.search(error) for pattern in patterns):
                return cause
        return None

    @staticmethod
    def _ineligible(voters: list[ModelVoterStatus]) -> EnumGateAcceptReason | None:
        if any(voter.blocking for voter in voters):
            return EnumGateAcceptReason.BLOCKING_FINDING_FROM_SUCCEEDED_VOTER
        lost = [voter for voter in voters if not voter.succeeded]
        if not lost:
            return EnumGateAcceptReason.NO_UNSUCCESSFUL_VOTER
        if any(voter.cause is None for voter in lost):
            return EnumGateAcceptReason.UNEXPLAINED_VOTER_FAILURE
        return None

    # -- the receipts --------------------------------------------------------

    def _receipts(
        self, request: ModelGateAcceptRequest, voters: list[ModelVoterStatus]
    ) -> ModelGateAcceptResult:
        try:
            reviews = self._reviews.reviews(request.repository, request.pull_request)
        except Exception:  # fail closed on any port failure
            return self._result(
                EnumGateAcceptDecision.REFUSE,
                EnumGateAcceptReason.REVIEWS_UNREADABLE,
                voters=voters,
            )
        policy, fence, _ = self._bind()
        publisher = policy.publisher_login.casefold()
        receipts: list[dict[str, Any]] = []
        malformed = 0
        for review in reviews:
            if review.author_login.casefold() != publisher:
                continue
            for block in fence.finditer(review.body):
                try:
                    receipt: Any = json.loads(block.group("body"))
                except ValueError:
                    malformed += 1
                    continue
                if isinstance(receipt, dict):
                    receipts.append(receipt)
                else:
                    malformed += 1
        if not receipts and not malformed:
            return self._result(
                EnumGateAcceptDecision.UNCHANGED,
                EnumGateAcceptReason.NO_RECEIPT_REVIEW,
                voters=voters,
            )
        refusals: list[str] = []
        for receipt in receipts:
            try:
                outcome = self._verifier.verify(
                    ModelReceiptVerifyRequest(
                        receipt=receipt,
                        repository=request.repository,
                        pull_request=request.pull_request,
                        head_sha=request.head_sha,
                        verifier_endpoint=policy.verifier_endpoint,
                    )
                )
            except Exception:  # fail closed on any port failure
                return self._result(
                    EnumGateAcceptDecision.REFUSE,
                    EnumGateAcceptReason.VERIFIER_UNREACHABLE,
                    voters=voters,
                )
            receipt_id = outcome.receipt_id or str(receipt.get("id", ""))
            if outcome.accepted and receipt_id:
                return self._result(
                    EnumGateAcceptDecision.PASS_AGENT,
                    EnumGateAcceptReason.RECEIPT_ACCEPTED,
                    receipt_id=receipt_id,
                    voters=voters,
                )
            reason = outcome.reason or "accepted_without_receipt_id"
            refusals.append(f"{receipt.get('id', '?')}={reason}")
        return self._result(
            EnumGateAcceptDecision.REFUSE,
            EnumGateAcceptReason.RECEIPT_REFUSED
            if refusals
            else EnumGateAcceptReason.RECEIPT_MALFORMED,
            voters=voters,
            refusals=refusals,
        )

    # -- the declared output -------------------------------------------------

    @staticmethod
    def _result(
        decision: EnumGateAcceptDecision,
        reason: EnumGateAcceptReason,
        *,
        receipt_id: str = "",
        voters: list[ModelVoterStatus] | None = None,
        refusals: list[str] | None = None,
    ) -> ModelGateAcceptResult:
        line = f"Hostile review gate accept: decision={decision.value} reason={reason.value}"
        if decision is EnumGateAcceptDecision.PASS_AGENT:
            line += f" source=agent receipt={receipt_id}"
        if refusals:
            line += f" refused: {', '.join(refusals)}"
        lost = [voter for voter in voters or [] if not voter.succeeded]
        if lost:
            causes = ", ".join(
                f"{voter.voter}={voter.cause.value if voter.cause else _UNTYPED}"
                for voter in lost
            )
            line += f"; unsuccessful voters: {causes}"
        return ModelGateAcceptResult(
            decision=decision,
            reason=reason,
            receipt_id=receipt_id,
            summary_line=line,
        )
