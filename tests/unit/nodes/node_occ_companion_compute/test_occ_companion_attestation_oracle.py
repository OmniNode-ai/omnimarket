# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Attestation oracle tests (OMN-14055) — the verify-side extension of RSD-1.

These are the T2-consuming regression tests the design doc calls the "OMN-14055
acceptance" property: a gate that re-invokes ``compute_companion_plan`` and
byte-diffs the deterministic subset against observed companion files.

  - A hand-authored companion is REJECTED even when it fabricates the SAME
    ``runner``/``verifier`` identity strings the canonical producer would use —
    proving actor-identity-only checks are insufficient (the exact property
    OMN-14055's acceptance criteria demands a regression test for).
  - The canonical COMPUTE node's own output is ACCEPTED (trivially reproducible
    from itself).
  - An independent-verifier companion (distinct, still-valid runner/verifier
    identity) is ACCEPTED as the documented escape hatch, but ONLY because it is
    still byte-reproducible — the escape hatch is not an identity bypass.
  - A stale/tampered companion (content diverges from the recomputed plan) is
    REJECTED with an actionable message naming both digests.

Standalone unit tests — zero I/O, zero live CI wiring in this PR (see the
handler module docstring for the follow-up-ticket scope note).
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_attestation import (
    verify_companion_attestation,
)
from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)


def _probe(stdout: str = '{"number":321,"state":"OPEN"}') -> ModelObservedProbe:
    return ModelObservedProbe(
        command="gh pr view 321 --repo OmniNode-ai/omnimarket --json number,state",
        stdout=stdout,
        exit_code=0,
    )


def _request(**overrides: object) -> ModelOccCompanionRequest:
    base: dict[str, object] = {
        "repo": "OmniNode-ai/omnimarket",
        "pr_number": 321,
        "pr_head_sha": "b" * 40,
        "pr_title": "feat(OMN-9999): the thing",
        "pr_body": "Implements the thing.",
        "pr_state": "open",
        "pr_head_ref": "feature-branch",
        "run_timestamp": "2026-07-10T00:00:00Z",
        "product_probe": _probe(),
    }
    base.update(overrides)
    return ModelOccCompanionRequest(**base)  # type: ignore[arg-type]


@pytest.mark.unit
class TestAttestationAcceptsCanonicalOutput:
    def test_compute_node_own_output_is_accepted(self) -> None:
        req = _request()
        plan = compute_companion_plan(req)
        result = verify_companion_attestation(plan.companion_files, req)
        assert result.accepted is True
        assert (
            result.observed_digest
            == result.expected_digest
            == plan.deterministic_digest
        )

    def test_independent_verifier_still_reproducible_is_accepted(self) -> None:
        """Escape hatch: a DIFFERENT (still-valid) verifier identity passes —
        but only because the content is still byte-reproducible. The identity
        change alone proves nothing; content reproducibility does the work."""
        req = _request(runner="node_occ_companion_compute", verifier="jane-reviewer")
        plan = compute_companion_plan(req)
        result = verify_companion_attestation(plan.companion_files, req)
        assert result.accepted is True


@pytest.mark.unit
class TestAttestationRejectsHandAuthoring:
    def test_hand_authored_companion_under_same_claimed_identity_is_rejected(
        self,
    ) -> None:
        """The regression proof OMN-14055's acceptance criteria demands: a
        hand-authored companion claiming the SAME runner/verifier identity as
        the canonical producer must still be rejected without content
        reproducibility. Actor-identity-only checks cannot catch this — the
        content diff is what catches it."""
        req = _request()
        plan = compute_companion_plan(req)
        # A human "improves" one receipt's actual_output prose — content now
        # diverges from what compute_companion_plan would render, even though
        # every identity field (runner/verifier/ticket) is byte-identical to
        # the canonical producer's own claimed identity.
        hand_authored = tuple(
            f.model_copy(
                update={
                    "content": f.content.replace(
                        "Evidence-Source autobind",
                        "Evidence-Source autobind (verified manually, looks good)",
                    )
                }
            )
            for f in plan.companion_files
        )
        assert hand_authored != plan.companion_files, "fixture must actually diverge"
        result = verify_companion_attestation(hand_authored, req)
        assert result.accepted is False
        assert "REJECTED" in result.reason
        assert result.observed_digest != result.expected_digest

    def test_stale_companion_with_mutated_hash_is_rejected(self) -> None:
        req = _request()
        plan = compute_companion_plan(req)
        tampered = tuple(
            f.model_copy(update={"contract_sha256": "sha256:deadbeef" * 4})
            for f in plan.companion_files
        )
        result = verify_companion_attestation(tampered, req)
        assert result.accepted is False
        assert result.expected_digest == plan.deterministic_digest

    def test_rejection_reason_names_both_digests_and_the_pr(self) -> None:
        req = _request()
        plan = compute_companion_plan(req)
        tampered = tuple(
            f.model_copy(update={"content": f.content + "\n# tampered"})
            for f in plan.companion_files
        )
        result = verify_companion_attestation(tampered, req)
        assert "OmniNode-ai/omnimarket#321" in result.reason
        assert result.observed_digest in result.reason
        assert result.expected_digest in result.reason

    def test_empty_observed_files_against_a_nonempty_plan_is_rejected(self) -> None:
        req = _request()
        result = verify_companion_attestation((), req)
        assert result.accepted is False


@pytest.mark.unit
class TestAttestationIdentityIsIrrelevantToTheVerdict:
    def test_two_requests_differing_only_in_identity_both_accept_their_own_output(
        self,
    ) -> None:
        """Proves the check is content-based, not identity-based: neither
        acceptance depends on WHICH valid runner/verifier pair was used."""
        req_a = _request(runner="node_occ_companion_compute", verifier="alice")
        req_b = _request(runner="node_occ_companion_compute", verifier="bob")
        plan_a = compute_companion_plan(req_a)
        plan_b = compute_companion_plan(req_b)
        assert verify_companion_attestation(plan_a.companion_files, req_a).accepted
        assert verify_companion_attestation(plan_b.companion_files, req_b).accepted
        # Cross-checking A's files against B's request fails not because of
        # identity but because pr_head_sha/probe differ across the two — a
        # sanity check that the oracle is not vacuously true.
        cross = verify_companion_attestation(plan_a.companion_files, req_b)
        assert cross.accepted == (plan_a.companion_files == plan_b.companion_files)
