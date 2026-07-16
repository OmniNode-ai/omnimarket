# RSD OCC-producer proof target (OMN-14695)

This document is a deliberate, minimal target for the RSD flywheel proof.

It exists so the canonical deterministic OCC-companion producer
(`node_occ_companion_effect`, RSD-3, OMN-14622) can be run in `mode=mutate`
against a real, open product PR and demonstrate that the machine-minted OCC
evidence companion passes `occ-preflight / eligibility` end-to-end — i.e. that
the OMN-14622 fix (self-bind rendered as a *declared contract dod_evidence
entry*, not merely a receipt) and the OMN-14550 fix (self-bind receipt bound to
the OCC PR number) hold when the real producer runs, not just in unit tests.

This is the unmet TRIGGER of OMN-14393 (RSD-5 fail-closed attestation gate):
">= 1 machine-minted OCC companion authored by `node_occ_companion_effect`
passes occ-preflight on a real, non-canary PR."

Not intended to change runtime behavior. The product PR is the proof target; the
machine mints the companion.
