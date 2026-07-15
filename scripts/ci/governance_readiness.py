#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Governance Readiness reason-code classifier (OMN-14646, WS3).

Root cause this module addresses
--------------------------------
`omnimarket` carries ~two dozen workflow files that independently re-run and
poll OCC-preflight / receipt / deploy governance state on every ``pull_request``
and ``merge_group`` event (the "OCC Preflight Dependency polling pattern" in the
merge-flow throughput plan, epic OMN-14643). Each copy decides on its own what a
skipped, cancelled, or failing governance subcheck *means*, so the merge-flow
signal is fragmented and there is no single, typed answer to the question
"is this candidate governance-ready, and if not, exactly why?".

WS3 consolidates that into ONE reusable Governance Readiness orchestration path
built *around* the existing validators — ``occ-preflight`` (eligibility),
``receipt-gate`` (verify), and ``deploy-gate`` — without reimplementing any of
them. This module is the deterministic, unit-tested core of that path: it takes
the already-computed conclusions of those subchecks plus the evidence facts they
surface and maps them to exactly one typed :class:`EnumGovernanceReasonCode`.

Design invariants
-----------------
- **No network I/O.** The workflow resolves subcheck conclusions and evidence
  facts (via ``gh`` / check-run annotations) and passes them in as facts; this
  module only classifies. (Mirrors ``scripts/ci/merge_queue_enqueue.py``.)
- **Fail closed.** ``READY`` is returned *only* when every governance-relevant
  subcheck is affirmatively green and the evidence facts confirm a fresh,
  valid receipt. A skipped, cancelled, timed-out, or absent subcheck can never
  yield ``READY`` — it maps to an explicit reason code, never a silent pass
  (per ``reference_ci_gate_enforcement_mechanics``).
- **Distinct, actionable diagnosis.** Missing evidence, stale (superseded-head)
  evidence, and a present-but-failing receipt produce *distinct* reason codes so
  the fix is unambiguous. A green-on-absence or green-on-wrong result is a bug,
  not a pass.
"""

from __future__ import annotations

import argparse
import json
import sys
from enum import StrEnum
from typing import Any


class EnumGovernanceReasonCode(StrEnum):
    """Typed governance-readiness outcome.

    ``READY`` is the single success value; every other value names why the
    candidate is not governance-ready. The non-``READY`` codes are the typed
    reason codes required by the merge-flow throughput plan (WS3).
    """

    READY = "READY"
    PRODUCT_NOT_GREEN = "PRODUCT_NOT_GREEN"
    POLICY_HELD = "POLICY_HELD"
    RUNNER_INFRA = "RUNNER_INFRA"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    RECEIPT_FAILED = "RECEIPT_FAILED"


class EnumSubcheckOutcome(StrEnum):
    """Coarse category a raw GitHub check conclusion maps to."""

    PASS = "pass"
    FAIL = "fail"
    INFRA = "infra"
    ABSENT = "absent"


# GitHub check-run ``conclusion`` values (and the merge-flow states we accept)
# bucketed into coarse outcomes. Anything not affirmatively ``success``/
# ``neutral`` is non-passing; the split between FAIL and INFRA determines
# whether the reason code is a governance defect or an infrastructure signal.
_PASS_CONCLUSIONS = frozenset({"success", "neutral"})
_FAIL_CONCLUSIONS = frozenset({"failure", "action_required"})
_INFRA_CONCLUSIONS = frozenset(
    {
        "cancelled",
        "canceled",
        "timed_out",
        "startup_failure",
        "stale",
        "skipped",  # a path-filtered/administrative skip is fail-closed, never a pass
    }
)
# Values that mean "no affirmative result exists yet" — pending, queued, or the
# subcheck simply never reported. Fail-closed: absence is never a pass.
_ABSENT_CONCLUSIONS = frozenset(
    {
        "",
        "none",
        "null",
        "pending",
        "queued",
        "in_progress",
        "waiting",
        "expected",
        "requested",
    }
)

# Evidence-state facts surfaced by occ-preflight (from its ``::error::``/
# annotation output), passed in by the workflow. ``present`` means an
# Evidence-Source resolved to a live OCC SHA; ``missing`` means no
# Evidence-Source / no companion; ``stale`` means evidence resolved but is bound
# to a superseded head or contract digest.
_EVIDENCE_STATES = frozenset({"present", "missing", "stale", "unknown"})
# Receipt-state facts surfaced by receipt-gate: ``pass`` (present + valid),
# ``fail`` (present but invalid/failing), ``absent`` (no receipt), ``unknown``.
_RECEIPT_STATES = frozenset({"pass", "fail", "absent", "unknown"})


def categorize_conclusion(conclusion: str | None) -> EnumSubcheckOutcome:
    """Map a raw GitHub check conclusion to a coarse :class:`EnumSubcheckOutcome`.

    Unknown/unexpected conclusions fail closed to ``INFRA`` (never ``PASS``).
    """
    value = (conclusion or "").strip().lower()
    if value in _PASS_CONCLUSIONS:
        return EnumSubcheckOutcome.PASS
    if value in _FAIL_CONCLUSIONS:
        return EnumSubcheckOutcome.FAIL
    if value in _ABSENT_CONCLUSIONS:
        return EnumSubcheckOutcome.ABSENT
    if value in _INFRA_CONCLUSIONS:
        return EnumSubcheckOutcome.INFRA
    # Fail closed: an unrecognized conclusion is treated as infra, not a pass.
    return EnumSubcheckOutcome.INFRA


def _truthy(value: Any) -> bool:
    """Coerce a JSON-ish value to bool without surprising falsy strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def _norm(value: str | None, allowed: frozenset[str], default: str) -> str:
    v = (value or "").strip().lower()
    return v if v in allowed else default


class GovernanceFacts:
    """The already-resolved facts the classifier consumes.

    All fields are supplied by the workflow (from ``gh`` / annotations); this
    class performs no I/O. ``product_conclusion`` is the aggregate Product
    Readiness / CI conclusion; the remaining conclusions are the governance
    subchecks the orchestrator wraps.
    """

    def __init__(
        self,
        *,
        product_conclusion: str | None = None,
        occ_conclusion: str | None = None,
        receipt_conclusion: str | None = None,
        deploy_conclusion: str | None = None,
        evidence_state: str | None = None,
        receipt_state: str | None = None,
        policy_held: Any = False,
    ) -> None:
        self.product = categorize_conclusion(product_conclusion)
        self.occ = categorize_conclusion(occ_conclusion)
        self.receipt = categorize_conclusion(receipt_conclusion)
        self.deploy = categorize_conclusion(deploy_conclusion)
        self.evidence_state = _norm(evidence_state, _EVIDENCE_STATES, "unknown")
        self.receipt_state = _norm(receipt_state, _RECEIPT_STATES, "unknown")
        self.policy_held = _truthy(policy_held)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GovernanceFacts:
        return cls(
            product_conclusion=data.get("product_conclusion"),
            occ_conclusion=data.get("occ_conclusion"),
            receipt_conclusion=data.get("receipt_conclusion"),
            deploy_conclusion=data.get("deploy_conclusion"),
            evidence_state=data.get("evidence_state"),
            receipt_state=data.get("receipt_state"),
            policy_held=data.get("policy_held", False),
        )


class GovernanceReadinessResult:
    """Typed classifier output."""

    def __init__(
        self, code: EnumGovernanceReasonCode, ready: bool, message: str
    ) -> None:
        self.code = code
        self.ready = ready
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason_code": str(self.code),
            "ready": self.ready,
            "message": self.message,
        }


def classify(facts: GovernanceFacts) -> GovernanceReadinessResult:
    """Classify governance readiness into exactly one typed reason code.

    Precedence (first match wins). The ordering is deterministic and documented
    so a single source revision — not each poller — decides behaviour:

    1. ``PRODUCT_NOT_GREEN`` — product is not affirmatively green. Product must be
       proven green before governance/evidence matters at all (product-first,
       per the plan). A product FAIL, INFRA, or ABSENT all block here: evidence
       bound to an unproven head is exactly what WS1/WS3 exist to prevent.
    2. ``POLICY_HELD`` — product is green but governance is *deliberately* held
       (e.g. deploy-gate awaiting a fresh CODEOWNERS grant, or an operator hold).
       A deliberate wait is not relabelled as a defect.
    3. ``EVIDENCE_MISSING`` / ``EVIDENCE_STALE`` — occ-preflight produced a
       *confirmed* FAIL verdict. This is a resolved, actionable diagnosis, so it
       outranks a merely-absent downstream subcheck: ``stale`` evidence →
       ``EVIDENCE_STALE``; otherwise ``EVIDENCE_MISSING``.
    4. ``RUNNER_INFRA`` — occ-preflight *itself* is INFRA or ABSENT (cancelled,
       timed-out, skipped, or never reported). Fail closed: we cannot *confirm*
       evidence state, so we must not forge an EVIDENCE_MISSING diagnosis and
       must never fall through to READY.
    5. ``EVIDENCE_STALE`` — occ passed on a cached head but the resolved binding
       is to a superseded head/contract digest.
    6. ``RECEIPT_FAILED`` — a receipt is present but invalid/failing (receipt-gate
       FAIL, or ``receipt_state == "fail"``).
    7. ``RUNNER_INFRA`` — a downstream governance subcheck (receipt/deploy) is
       INFRA or ABSENT. Fail closed on the remaining subchecks.
    8. ``RECEIPT_FAILED`` — no valid receipt is present (``receipt_state`` absent).
    9. ``READY`` — everything above is satisfied.
    """
    # 1. Product gate — product must be affirmatively green first.
    if facts.product is not EnumSubcheckOutcome.PASS:
        return GovernanceReadinessResult(
            EnumGovernanceReasonCode.PRODUCT_NOT_GREEN,
            ready=False,
            message=(
                "Product Readiness is not green "
                f"(outcome={facts.product}); governance evidence must not be "
                "bound to an unproven product head."
            ),
        )

    # 2. Deliberate policy hold (approval/grant/operator hold) — not a defect.
    if facts.policy_held:
        return GovernanceReadinessResult(
            EnumGovernanceReasonCode.POLICY_HELD,
            ready=False,
            message="Governance is held pending an external policy/approval decision.",
        )

    # 3. Confirmed occ-preflight FAIL verdict — distinguish stale vs missing.
    # A resolved occ FAILURE is an actionable diagnosis and outranks a downstream
    # subcheck that merely never reported (which would only yield RUNNER_INFRA).
    if facts.occ is EnumSubcheckOutcome.FAIL:
        if facts.evidence_state == "stale":
            return GovernanceReadinessResult(
                EnumGovernanceReasonCode.EVIDENCE_STALE,
                ready=False,
                message=(
                    "OCC evidence resolved but is bound to a superseded head or "
                    "contract digest; the frozen tuple no longer matches."
                ),
            )
        # missing / unknown / present all reduce to MISSING here: occ-preflight's
        # dominant failure mode is an absent/unusable Evidence-Source.
        return GovernanceReadinessResult(
            EnumGovernanceReasonCode.EVIDENCE_MISSING,
            ready=False,
            message=(
                "OCC preflight failed with no usable Evidence-Source / OCC "
                "companion for this head."
            ),
        )

    # 4. occ-preflight itself did not produce a confirmable result — fail closed.
    if facts.occ in (EnumSubcheckOutcome.INFRA, EnumSubcheckOutcome.ABSENT):
        return GovernanceReadinessResult(
            EnumGovernanceReasonCode.RUNNER_INFRA,
            ready=False,
            message=(
                "occ-preflight did not produce a confirmable result "
                f"(outcome={facts.occ}); failing closed — a skipped, cancelled, "
                "or absent eligibility check is never a pass."
            ),
        )

    # 5. occ passed, but a stale evidence fact must still be rejected (green-on-
    # wrong: cached-head PASS with a superseded binding).
    if facts.evidence_state == "stale":
        return GovernanceReadinessResult(
            EnumGovernanceReasonCode.EVIDENCE_STALE,
            ready=False,
            message="OCC evidence is bound to a superseded head/contract digest.",
        )

    # 6. Receipt present but failing/invalid — confirmed verdict.
    if facts.receipt is EnumSubcheckOutcome.FAIL or facts.receipt_state == "fail":
        return GovernanceReadinessResult(
            EnumGovernanceReasonCode.RECEIPT_FAILED,
            ready=False,
            message="Receipt Gate failed: receipt is present but invalid or failing.",
        )

    # 7. Downstream governance subchecks (receipt/deploy) unconfirmable — fail closed.
    infra_subchecks = [
        name
        for name, outcome in (
            ("receipt-gate", facts.receipt),
            ("deploy-gate", facts.deploy),
        )
        if outcome in (EnumSubcheckOutcome.INFRA, EnumSubcheckOutcome.ABSENT)
    ]
    if infra_subchecks:
        return GovernanceReadinessResult(
            EnumGovernanceReasonCode.RUNNER_INFRA,
            ready=False,
            message=(
                "Governance subcheck(s) did not produce a confirmable result "
                f"({', '.join(infra_subchecks)}); failing closed — a skipped, "
                "cancelled, or absent subcheck is never a pass."
            ),
        )

    # 8. No valid receipt present.
    if facts.receipt_state == "absent":
        return GovernanceReadinessResult(
            EnumGovernanceReasonCode.RECEIPT_FAILED,
            ready=False,
            message="No valid receipt is present for this frozen head.",
        )

    # 9. Everything affirmatively green.
    return GovernanceReadinessResult(
        EnumGovernanceReasonCode.READY,
        ready=True,
        message="Product and Governance Readiness are green for the frozen tuple.",
    )


def classify_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Convenience: classify from a plain fact dict, return a plain result dict."""
    return classify(GovernanceFacts.from_dict(data)).to_dict()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Governance Readiness reason-code classifier (WS3, OMN-14646)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_classify = sub.add_parser("classify", help="Classify governance readiness")
    src = p_classify.add_mutually_exclusive_group(required=True)
    src.add_argument("--facts-json", help="JSON object of governance facts")
    src.add_argument(
        "--facts-file", help="Path to a file containing the facts JSON object"
    )
    p_classify.add_argument(
        "--report-only",
        default="true",
        help="When true (default), always exit 0 and only report the reason code.",
    )

    args = parser.parse_args(argv)

    if args.command == "classify":
        if args.facts_file:
            with open(args.facts_file, encoding="utf-8") as fh:
                data = json.load(fh)
        else:
            data = json.loads(args.facts_json)
        if not isinstance(data, dict):
            print("facts JSON must be an object", file=sys.stderr)
            return 2
        result = classify(GovernanceFacts.from_dict(data))
        print(json.dumps(result.to_dict()))
        report_only = _truthy(args.report_only)
        if report_only or result.ready:
            return 0
        # Enforcement mode (future, post-observation): non-ready is a hard fail.
        return 1

    parser.error(f"unknown command: {args.command}")
    return 2  # pragma: no cover - argparse exits first


if __name__ == "__main__":
    raise SystemExit(main())
