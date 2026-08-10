# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Pure evaluation of the RECEIPT_BOUND no-PR evidence class (OMN-15817 shape 5).

A ticket whose Done-proof is a durable OCC receipt trail with no product PR at
all — a read-only audit, an investigation, a live query readback — can satisfy
neither the merged-PR ``CONTRACT_CITES_MERGE_COMMIT`` check (no PR exists) nor
the ``RUNTIME_OPS_READBACK`` check (no runtime mutation occurred; the evidence
IS the observation itself). Doctrine already names this proof class explicitly
(``proof_class in {code-only, receipt-bound, deployed, live-readback,
replay-proven, prod-proven}``, ``omni_home/CLAUDE.md`` "Proof capacity" rule),
but prior to this change ``DurableEvidenceGate`` had no flip path for it — a
receipt-bound ticket such as OMN-15087 (a Postgres RLS role/connection audit,
zero product PR, only durable receipts) was permanently BLOCKED at Check 2
with no route to PASS.

This module implements the receipt-bound alternative to Check 2, discriminated
by a top-level ``proof_class: "receipt-bound"`` contract field — the SAME
vocabulary doctrine already uses — rather than a new receipt-level
``evidence_class`` enum member: adding an ``omnibase_core``
``EnumEvidenceClass`` member is cross-repo scope this fix does not take on,
and a contract-level field is local to ``omnimarket`` and mirrors the
existing ``prevention_gate``/``non_recurrence_note`` top-level contract
fields Check 4 already reads (see :func:`extract_defect_prevention` in
``durable_evidence_gate.py``).

Guardrails (mirrors the ``RUNTIME_OPS_READBACK`` G1/G3 discipline, OMN-14168):

* R1 — ``verifier != runner`` on every PASS receipt (independent attester); a
        self-attested receipt is refused.
* R2 — ``probe_stdout`` (the observed evidence) is non-empty; an empty
        readback is indistinguishable from a probe that never ran.
* R3 — at least one PASS receipt exists; a receipt-bound contract with zero
        PASS receipts has nothing to verify and fails closed.

Deliberately NARROWER than ``RUNTIME_OPS_READBACK``: no mutation-verb
allowlist (nothing mutates), no prod-target waiver-refusal (nothing is
promoted), no ``prevention_followup`` requirement (no repair-to-ratchet class
applies to a pure observation) — it proves only that independently-verified,
non-empty evidence was durably recorded, matching the task's own "everything
else unchanged" scope.

Flagged for operator ratification: this module implements a proof class
doctrine already names but that no prior gate enforced (OMN-15087 is the
motivating, currently-blocked example). See the introducing PR body.

Pure logic — no I/O, no env reads.
"""

from __future__ import annotations

RECEIPT_BOUND_PROOF_CLASS = "receipt-bound"


def _str_field(receipt: dict[str, object], key: str) -> str:
    value = receipt.get(key)
    return value.strip() if isinstance(value, str) else ""


def is_receipt_bound_contract(contract: dict[str, object]) -> bool:
    """True when the contract declares itself ``proof_class == "receipt-bound"``.

    This is the branch discriminator: when it holds, the gate runs
    :func:`evaluate_receipt_bound` in place of the merged-PR check. Pure
    function — no I/O.
    """
    value = contract.get("proof_class")
    return isinstance(value, str) and value.strip() == RECEIPT_BOUND_PROOF_CLASS


def evaluate_receipt_bound(
    pass_receipts: list[dict[str, object]],
) -> tuple[bool, str, set[tuple[str, str]]]:
    """Verify a receipt-bound PASS-receipt set against R1-R3.

    Args:
        pass_receipts: the ``status == PASS`` receipt payloads (post-
            supersession) tracked for the ticket. The caller has already
            filtered to PASS.

    Returns:
        ``(passed, message, receipt_keys)`` — same shape as
        :func:`evaluate_runtime_ops_readback`. ``receipt_keys`` is the set of
        ``(evidence_item_id, check_type)`` covered by the verified receipts,
        fed to the ``CONTRACT_ON_OCC_MAIN`` check ("whose contract binds the
        ticket").

    Pure function — no I/O.
    """
    if not pass_receipts:
        return (
            False,
            "No PASS receipt present for this receipt-bound ticket — nothing "
            "to verify (OMN-15817 shape 5, R3).",
            set(),
        )

    receipt_keys: set[tuple[str, str]] = set()
    for receipt in pass_receipts:
        runner = _str_field(receipt, "runner")
        verifier = _str_field(receipt, "verifier")
        # R1 — independent attester.
        if not runner or not verifier or runner == verifier:
            return (
                False,
                (
                    "Receipt-bound evidence receipt is self-attested "
                    "(verifier == runner) or missing an identity — an "
                    "independent verifier is required (OMN-15817 shape 5, "
                    "R1)."
                ),
                set(),
            )
        # R2 — non-empty observed evidence.
        if not _str_field(receipt, "probe_stdout"):
            return (
                False,
                (
                    "Receipt-bound evidence probe_stdout is empty — an empty "
                    "readback is indistinguishable from a probe that never "
                    "ran (OMN-15817 shape 5, R2)."
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
            f"All {len(pass_receipts)} receipt-bound evidence receipt(s) "
            "verified: independent verifier, non-empty observed evidence "
            "(OMN-15817 shape 5)."
        ),
        receipt_keys,
    )


__all__ = [
    "RECEIPT_BOUND_PROOF_CLASS",
    "evaluate_receipt_bound",
    "is_receipt_bound_contract",
]
