# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14785 (parent OMN-14783): OccCompanionEmitter-removal parity, step 1.

Two behaviors the canonical ``node_occ_companion_compute`` producer must match
before the bespoke ``OccCompanionEmitter`` is retired:

  * **F-17 label suppression.** The emitter suppresses a companion when the
    product PR carries a do-not-merge/WIP LABEL. ``compute_companion_plan`` now
    honours ``request.pr_labels`` in addition to the title/body markers it
    already checked, so a reviewer-applied ``do-not-merge`` label (which the PR
    author cannot edit away in the body) suppresses authoring.

  * **Reintroduction guard (RED against a bespoke/second producer).** The whole
    point of the migration is that exactly ONE producer may mint an OCC
    companion. The RSD-5 attestation oracle (``verify_companion_attestation``)
    is that guard: it ACCEPTS the canonical COMPUTE node's own output and
    REJECTS any bespoke/second producer's bytes (a hand-authored or stale
    companion), because acceptance is a pure function of byte-reproducibility
    from ``compute_companion_plan`` — never of the ``runner``/``verifier``
    identity a second producer could forge. This is the structural check that
    makes a reintroduced bespoke minter unable to pass as canonical.
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


def _probe() -> ModelObservedProbe:
    return ModelObservedProbe(
        command="gh pr view 321 --repo OmniNode-ai/omnimarket --json number,state",
        stdout='{"number":321,"state":"OPEN"}',
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
class TestF17LabelSuppression:
    def test_control_no_label_authors_a_companion(self) -> None:
        """RED-control: without a do-not-merge label the SAME request authors a
        non-empty companion — so the suppression tests below are load-bearing."""
        plan = compute_companion_plan(_request(pr_labels=()))
        assert plan.no_op is False
        assert plan.companion_files != ()

    @pytest.mark.parametrize(
        "label",
        ["do-not-merge", "do not merge", "DoNotMerge", "DNM", "WIP", "wip"],
    )
    def test_do_not_merge_label_suppresses(self, label: str) -> None:
        plan = compute_companion_plan(_request(pr_labels=(label,)))
        assert plan.no_op is True
        assert plan.companion_files == ()
        assert "label" in plan.no_op_reason.lower()

    def test_benign_label_does_not_suppress(self) -> None:
        plan = compute_companion_plan(_request(pr_labels=("bug", "area:ci")))
        assert plan.no_op is False
        assert plan.companion_files != ()

    def test_do_not_merge_label_wins_even_with_clean_title_and_body(self) -> None:
        """The label is the reviewer-applied hold marker: it suppresses even when
        the title/body carry no do-not-merge text (the exact case a body-only
        check would miss)."""
        plan = compute_companion_plan(
            _request(
                pr_title="feat(OMN-9999): ready to ship",
                pr_body="All green.",
                pr_labels=("do-not-merge",),
            )
        )
        assert plan.no_op is True
        assert plan.companion_files == ()


@pytest.mark.unit
class TestReintroductionGuardRejectsSecondProducer:
    """The migration's single-producer invariant, proven by the RSD-5 oracle."""

    def test_canonical_output_is_accepted(self) -> None:
        req = _request()
        plan = compute_companion_plan(req)
        result = verify_companion_attestation(plan.companion_files, req)
        assert result.accepted is True

    def test_bespoke_second_producer_output_is_rejected(self) -> None:
        """A second producer minting its OWN companion bytes (here modelled as a
        bespoke author that tweaks one receipt's prose while forging the SAME
        runner/verifier identity the canonical producer uses) is REJECTED — the
        reintroduction guard the emitter removal relies on. Byte-reproducibility,
        not identity, is what the oracle checks."""
        req = _request()
        plan = compute_companion_plan(req)
        bespoke = tuple(
            f.model_copy(
                update={
                    "content": f.content.replace(
                        "Evidence-Source autobind",
                        "Evidence-Source autobind (authored by a second minter)",
                    )
                }
            )
            for f in plan.companion_files
        )
        assert bespoke != plan.companion_files, "fixture must actually diverge"
        result = verify_companion_attestation(bespoke, req)
        assert result.accepted is False
        assert "REJECTED" in result.reason


@pytest.mark.unit
class TestCanonicalCompanionIsPlaceholderFree:
    def test_no_pending_sentinel_in_any_file(self) -> None:
        """Parity superiority: the emitter renders ``sha256:PENDING`` then rebinds
        (a missed rebind ships PENDING); the canonical producer bakes the real
        hash at render time and never emits the sentinel."""
        plan = compute_companion_plan(_request())
        assert plan.companion_files != ()
        for f in plan.companion_files:
            assert "PENDING" not in f.content, f"{f.path} carries a PENDING sentinel"
