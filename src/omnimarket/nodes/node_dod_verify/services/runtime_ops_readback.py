# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Pure evaluation of the RUNTIME_OPS_READBACK no-PR evidence class (OMN-14168).

A genuine, independently-verified, no-source-change runtime-ops fix (a
``kubectl patch``, a live restart, a live config repair) produces NO repo diff
and NO PR, so it can never satisfy the merged-PR ``CONTRACT_CITES_MERGE_COMMIT``
check. This module implements the alternative: given the raw PASS receipt
payloads tracked under a ticket's receipt directory on the OCC governance ref,
it decides whether they form a well-formed RUNTIME_OPS readback set and, if so,
whether every guardrail holds.

Guardrails (design 2026-07-08-nopr-runtime-ops-evidence-class.md):

* G1  — ``verifier != runner`` on every receipt (independent attester). A
        self-attested receipt is refused here in addition to the
        ``ModelDodReceipt`` PASS->ADVISORY downgrade, so a hand-forged
        ``status="PASS"`` payload cannot slip through.
* G2a — no ``pr_number`` / ``pr_url`` binding (a PR routes through the merged-PR
        gate); mixing RUNTIME_OPS with non-runtime-ops PASS receipts is refused
        as mis-declared.
* G2b — ``mutation_verb`` is in the GOVERNED runtime-ops verb allowlist
        (``omnibase_core`` ``runtime_ops_verb_allowlist.yaml``); ``git`` and
        image-digest promotion are excluded.
* G3  — ``no_source_change`` is True and ``probe_stdout`` (the readback) is
        non-empty; optional freshness check when a clock + window are supplied.
* G4  — a **prod** ``target_identity`` is NOT waived: the OMN-13418
        prod-promotion grant is a separate, un-forgeable gate this pure branch
        does not resolve, so a prod target fails closed here.
* G5  — every receipt links a ``prevention_followup`` recurrence ratchet.

This module is pure logic — no I/O, no env reads. The live re-read of runtime
state (Surface B) and the tick that mints the verifier receipt live elsewhere.
"""

from __future__ import annotations

from datetime import datetime

from omnibase_core.enums.governance.enum_evidence_class import EnumEvidenceClass

RUNTIME_OPS_EVIDENCE_CLASS = EnumEvidenceClass.RUNTIME_OPS.value
RUNTIME_OPS_CHECK_TYPE = "runtime_readback"


def _receipt_class(receipt: dict[str, object]) -> str:
    value = receipt.get("evidence_class")
    return value if isinstance(value, str) else ""


def _str_field(receipt: dict[str, object], key: str) -> str:
    value = receipt.get(key)
    return value.strip() if isinstance(value, str) else ""


def is_runtime_ops_receipt_set(receipts: list[dict[str, object]]) -> bool:
    """True when at least one PASS receipt declares ``evidence_class=RUNTIME_OPS``.

    This is the branch discriminator: when it holds, the gate runs
    :func:`evaluate_runtime_ops_readback` in place of the merged-PR check. Pure
    function — no I/O.
    """
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        status = receipt.get("status")
        if not isinstance(status, str) or status.upper() != "PASS":
            continue
        if _receipt_class(receipt) == RUNTIME_OPS_EVIDENCE_CLASS:
            return True
    return False


def is_prod_target(target_identity: str) -> bool:
    """True when ``target_identity`` names a prod lane / namespace / project.

    Token-exact match on ``prod`` so ``onex-prod`` / ``omnibase-infra-prod`` /
    ``prod`` match but ``product`` does not. Pure function.
    """
    tokens = set(
        "".join(c if c.isalnum() else " " for c in target_identity.lower()).split()
    )
    return "prod" in tokens


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def evaluate_runtime_ops_readback(
    pass_receipts: list[dict[str, object]],
    *,
    verb_allowlist: frozenset[str],
    now: datetime | None = None,
    max_age_seconds: float | None = None,
) -> tuple[bool, str, set[tuple[str, str]]]:
    """Verify a RUNTIME_OPS readback PASS-receipt set against every guardrail.

    Args:
        pass_receipts: the ``status == PASS`` receipt payloads (post-supersession)
            tracked for the ticket. The caller has already filtered to PASS.
        verb_allowlist: the governed runtime-ops verb allowlist (G2b).
        now: optional clock for the freshness check. When ``None`` (the default,
            used by the pure Surface-A gate) staleness is NOT checked here — the
            Surface-B probe re-reads live state instead.
        max_age_seconds: optional freshness window; only honored when ``now`` is
            supplied.

    Returns:
        ``(passed, message, receipt_keys)`` where ``receipt_keys`` is the set of
        ``(evidence_item_id, check_type)`` covered by the verified runtime-ops
        receipts (fed to the contract-on-OCC check). On refusal ``passed`` is
        ``False`` and ``receipt_keys`` is empty.

    Pure function — no I/O.
    """
    runtime_ops = [
        r
        for r in pass_receipts
        if isinstance(r, dict) and _receipt_class(r) == RUNTIME_OPS_EVIDENCE_CLASS
    ]
    if not runtime_ops:
        # Caller only invokes this when is_runtime_ops_receipt_set() is True, so
        # this is a defensive fail-closed guard, not a normal path.
        return (
            False,
            "No RUNTIME_OPS PASS receipt present — nothing to verify.",
            set(),
        )

    # G2a mixing: a runtime-ops close must not be padded with PR-bound work.
    non_runtime = [
        r
        for r in pass_receipts
        if isinstance(r, dict) and _receipt_class(r) != RUNTIME_OPS_EVIDENCE_CLASS
    ]
    if non_runtime:
        return (
            False,
            (
                "Mis-declared receipt set: it mixes RUNTIME_OPS receipt(s) with "
                f"{len(non_runtime)} non-runtime-ops PASS receipt(s). A runtime-ops "
                "close must be pure no-PR ops; work with a PR routes through the "
                "merged-PR gate (OMN-14168, G2a)."
            ),
            set(),
        )

    receipt_keys: set[tuple[str, str]] = set()
    for receipt in runtime_ops:
        runner = _str_field(receipt, "runner")
        verifier = _str_field(receipt, "verifier")
        # G1 — independent attester (also rejects a hand-forged status=PASS that
        # bypassed the model's PASS->ADVISORY self-attestation downgrade).
        if not runner or not verifier or runner == verifier:
            return (
                False,
                (
                    "RUNTIME_OPS receipt is self-attested (verifier == runner) or "
                    "missing an identity — an independent verifier is required "
                    "(OMN-14168, G1)."
                ),
                set(),
            )
        # G2a — no PR / source binding.
        if receipt.get("pr_number") is not None or _str_field(receipt, "pr_url"):
            return (
                False,
                (
                    "RUNTIME_OPS receipt carries a pr_number/pr_url — that work has "
                    "a PR and must route through the merged-PR gate, not the "
                    "runtime-ops class (OMN-14168, G2a)."
                ),
                set(),
            )
        # G3 — no source change asserted.
        if receipt.get("no_source_change") is not True:
            return (
                False,
                (
                    "RUNTIME_OPS receipt does not assert no_source_change=True — the "
                    "class is bounded to genuine no-source-change ops (OMN-14168)."
                ),
                set(),
            )
        # G2b — mutation verb in the governed allowlist.
        verb = _str_field(receipt, "mutation_verb")
        if verb not in verb_allowlist:
            return (
                False,
                (
                    f"RUNTIME_OPS mutation_verb {verb!r} is not in the governed "
                    f"runtime-ops verb allowlist {sorted(verb_allowlist)}. A source "
                    "change (git / image-digest promotion) produces a PR and must "
                    "route through the merged-PR gate (OMN-14168, G2b)."
                ),
                set(),
            )
        # G3 — readback stdout must be non-empty.
        if not _str_field(receipt, "probe_stdout"):
            return (
                False,
                (
                    "RUNTIME_OPS readback probe_stdout is empty — an empty readback "
                    "is indistinguishable from a probe that never ran (OMN-14168, "
                    "G3)."
                ),
                set(),
            )
        # G5 — recurrence-prevention ratchet.
        if not _str_field(receipt, "prevention_followup"):
            return (
                False,
                (
                    "RUNTIME_OPS receipt links no prevention_followup — every no-PR "
                    "runtime-ops close must link a GitOps/declarative guardrail "
                    "follow-up so the hatch cannot become a permanent bypass "
                    "(OMN-14168, G5)."
                ),
                set(),
            )
        # G4 — prod is NOT waived. This pure branch does not resolve the
        # OMN-13418 prod-promotion grant, so a prod target fails closed here;
        # prod runtime recovery must clear the dedicated prod-promotion gate.
        target = _str_field(receipt, "target_identity")
        if is_prod_target(target):
            return (
                False,
                (
                    f"RUNTIME_OPS target_identity {target!r} is a prod lane. The "
                    "merged-PR waiver does NOT waive the OMN-13418 prod-promotion "
                    "grant; a prod runtime-ops close must clear that gate "
                    "separately and fails closed here (OMN-14168, G4)."
                ),
                set(),
            )
        # Optional Surface-A freshness check (Surface B re-reads live state).
        if now is not None and max_age_seconds is not None:
            ts = _parse_timestamp(receipt.get("run_timestamp"))
            if ts is None or (now - ts).total_seconds() > max_age_seconds:
                return (
                    False,
                    (
                        "RUNTIME_OPS readback is stale — its run_timestamp is "
                        "outside the freshness window; re-run the read-only probe "
                        "against live state (OMN-14168, G3/abuse #6)."
                    ),
                    set(),
                )

        evidence_item_id = receipt.get("evidence_item_id")
        check_type = receipt.get("check_type")
        if isinstance(evidence_item_id, str) and isinstance(check_type, str):
            receipt_keys.add((evidence_item_id, check_type))

    return (
        True,
        (
            f"All {len(runtime_ops)} RUNTIME_OPS readback receipt(s) verified: "
            "independent verifier, no PR, allowlisted mutation verb, non-empty "
            "readback, prevention follow-up linked, non-prod target."
        ),
        receipt_keys,
    )


__all__ = [
    "RUNTIME_OPS_CHECK_TYPE",
    "RUNTIME_OPS_EVIDENCE_CLASS",
    "evaluate_runtime_ops_readback",
    "is_prod_target",
    "is_runtime_ops_receipt_set",
]
