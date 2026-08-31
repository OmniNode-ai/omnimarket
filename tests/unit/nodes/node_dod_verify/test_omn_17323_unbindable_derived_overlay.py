# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17323: an unbindable auto-derived ``::pr-live-state`` overlay must not
sit in the verdict-bearing denominator.

The OMN-16106 autoclose mechanism runs every 30 minutes and had, at the time
this was measured, never flipped a ticket: 24 consecutive scheduled runs
(``33342059445`` -> ``33387386898``, 2026-08-30/31), ~660 companion scans,
FLIP=0 in every run.

The reason is arithmetic, not evidence. Diagnostic dispatch run
``33389305481`` on OMN-17313 reported::

    status=verified total=8 verified=3 failed=0 skipped=1 superseded=0
    non_probative=4 behavior_proving=1

``node_dod_verify``'s own terminal status is ``verified``, zero failed, one
behavior-proving check. The autoclose flip predicate
(``handler_evidence_autoclose_sweep.py``, ``all_verified``) still refuses it,
because ``verified_count + non_probative_count == total_checks`` reads
``3 + 4 = 7 != 8``.

The single unaccounted check is::

    [skipped] dod-occ-diff-derived-behavior-proof::pr-live-state:
      NO_CONSISTENT_PR_BINDING: item '...' has a PASS receipt recording
      pr_number, but no receipt field consistently pairs that number with a
      repo — no live-state binding derived.

That check is **not a criterion the ticket declared**. It is an overlay the
*verifier* auto-derives for every evidence item
(``EvidenceCollector._live_pr_checks_for_item``, the ``not bindings and
self._last_binding_note is not None`` arm), and it skipped because the
verifier's own binder could not derive a binding. It is the tool reporting its
own inability, attributed to the ticket as an evidence shortfall — and since
OMN-16434 auto-mints ``dod-occ-diff-derived-behavior-proof`` onto every new OCC
companion, every freshly-minted companion carries exactly one guaranteed
unbindable overlay and the equality can never hold.

The governing precedent is OMN-15390, already in ``HandlerDodVerify``:
``total_checks`` is the VERDICT-BEARING denominator, and ``superseded`` is
excluded from it because an entry carrying no product-dependent verdict does
not belong in a verdict-bearing denominator. An unbindable derived overlay was
never executed and can never pass — the same class, one axis over.

Deliberately NOT widened (pinned by
:func:`test_other_skipped_shapes_still_break_the_equality`): a live-PR-check
disabled skip, an OMN-16087 intentional non-merged assertion, and an OMN-16788
``unverifiable_cause`` skip all keep blocking on their existing terms. Only the
one arm where the verifier's own binder returned nothing is affected.

RED-before evidence (run against unmodified ``dev``, this file only):
``test_omn_17313_counters_clear_the_autoclose_flip_predicate`` failed with
``total_checks == 8`` (expected 7), and
``test_unbindable_overlay_carries_a_typed_marker_not_message_text`` failed at
import/attribute time — ``ModelEvidenceCheckResult`` had no such field.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml

from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    EnumEvidenceUnverifiableCause,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

TICKET = "OMN-17313"


def _command() -> ModelDodVerifyStartCommand:
    return ModelDodVerifyStartCommand(correlation_id=uuid.uuid4(), ticket_id=TICKET)


def _check(
    evidence_id: str,
    status: EnumEvidenceCheckStatus,
    *,
    proof_class: EnumCheckProofClass = EnumCheckProofClass.INDETERMINATE,
    unbindable_derived_overlay: bool = False,
    unverifiable_cause: EnumEvidenceUnverifiableCause | None = None,
) -> ModelEvidenceCheckResult:
    return ModelEvidenceCheckResult(
        evidence_id=evidence_id,
        description=f"check {evidence_id}",
        status=status,
        proof_class=proof_class,
        unbindable_derived_overlay=unbindable_derived_overlay,
        unverifiable_cause=unverifiable_cause,
    )


def _omn_17313_shaped_checks() -> list[ModelEvidenceCheckResult]:
    """Reproduce run 33389305481's exact counter shape for OMN-17313.

    3 verified (one of them behavior-proving), 4 non-probative, 1 skipped —
    and the single skip is the verifier's own unbindable derived overlay.
    """
    checks = [
        _check(
            "dod-occ-diff-derived-behavior-proof",
            EnumEvidenceCheckStatus.VERIFIED,
            proof_class=EnumCheckProofClass.BEHAVIOR,
        ),
        _check(
            "dod-OmniNode-ai-omniclaude-pr-2093::pr-live-state",
            EnumEvidenceCheckStatus.VERIFIED,
            proof_class=EnumCheckProofClass.MERGE_STATE,
        ),
        _check(
            "dod-OmniNode-ai-omniclaude-pr-2093",
            EnumEvidenceCheckStatus.VERIFIED,
            proof_class=EnumCheckProofClass.MERGE_STATE,
        ),
    ]
    checks += [
        _check(
            f"dod-surrogate-{n}",
            EnumEvidenceCheckStatus.NON_PROBATIVE,
            proof_class=EnumCheckProofClass.SURROGATE,
        )
        for n in range(4)
    ]
    checks.append(
        _check(
            "dod-occ-diff-derived-behavior-proof::pr-live-state",
            EnumEvidenceCheckStatus.SKIPPED,
            proof_class=EnumCheckProofClass.MERGE_STATE,
            unbindable_derived_overlay=True,
        )
    )
    return checks


@pytest.mark.unit
def test_omn_17313_counters_clear_the_autoclose_flip_predicate() -> None:
    """The measured OMN-17313 shape must satisfy the flip equality.

    RED before the fix: ``total_checks`` was 8 and ``3 + 4 != 8``.
    """
    checks = _omn_17313_shaped_checks()
    assert len(checks) == 8, "fixture must reproduce the measured 8 raw checks"

    state = HandlerDodVerify()._handle_typed(_command(), checks)

    assert state.status is EnumDodVerifyStatus.VERIFIED
    assert state.failed_count == 0
    assert state.verified_count == 3
    assert state.non_probative_count == 4
    assert state.behavior_proving_count == 1
    # The overlay leaves the denominator, exactly as ``superseded`` does.
    assert state.total_checks == 7
    assert state.unbindable_overlay_count == 1
    # The exact conjunction ``handler_evidence_autoclose_sweep.all_verified``
    # evaluates, kept whole (bound, not split) because it is the single
    # predicate this ticket is about — splitting it into five asserts would
    # stop mirroring the consumer.
    all_verified = (
        state.status is EnumDodVerifyStatus.VERIFIED
        and state.total_checks > 0
        and state.failed_count == 0
        and state.verified_count > 0
        and state.verified_count + state.non_probative_count == state.total_checks
    )
    assert all_verified, "the autoclose flip predicate must now be satisfied"


@pytest.mark.unit
def test_the_overlay_stays_in_checks_with_its_diagnostic_note() -> None:
    """Excluded from the denominator, never hidden from the receipt."""
    state = HandlerDodVerify()._handle_typed(_command(), _omn_17313_shaped_checks())

    overlays = [c for c in state.checks if c.unbindable_derived_overlay]
    assert len(overlays) == 1
    assert overlays[0].evidence_id.endswith("::pr-live-state")
    assert overlays[0].status is EnumEvidenceCheckStatus.SKIPPED
    assert len(state.checks) == 8, "the entry is retained, not dropped"


@pytest.mark.unit
def test_unbindable_overlay_carries_a_typed_marker_not_message_text() -> None:
    """The marker is a model field; no consumer parses ``NO_CONSISTENT_PR_BINDING``.

    OMN-16788's own contract for ``unverifiable_cause`` — "consumers must branch
    on this field, never on message text" — applied one axis over.
    """
    result = _check(
        "item::pr-live-state",
        EnumEvidenceCheckStatus.SKIPPED,
        unbindable_derived_overlay=True,
    )
    assert result.unbindable_derived_overlay is True
    # It is NOT an OMN-16788 unverifiable cause: nothing was unreadable, the
    # binder simply derived nothing.
    assert result.unverifiable_cause is None

    dumped = result.model_dump(mode="json")
    assert dumped["unbindable_derived_overlay"] is True


@pytest.mark.unit
def test_marker_is_rejected_on_a_non_skipped_result() -> None:
    """A marker asserting "never executed" may not sit on a status saying it ran.

    Structural, mirroring ``_cause_requires_skipped`` (OMN-16788).
    """
    for status in (
        EnumEvidenceCheckStatus.VERIFIED,
        EnumEvidenceCheckStatus.FAILED,
        EnumEvidenceCheckStatus.NON_PROBATIVE,
        EnumEvidenceCheckStatus.SUPERSEDED,
    ):
        with pytest.raises(ValueError, match="unbindable_derived_overlay"):
            ModelEvidenceCheckResult(
                evidence_id="item::pr-live-state",
                description="d",
                status=status,
                unbindable_derived_overlay=True,
            )


@pytest.mark.unit
def test_other_skipped_shapes_still_break_the_equality() -> None:
    """AC4: the three unmarked SKIPPED shapes keep gapping, unchanged.

    Live-check-disabled, the OMN-16087 asserted non-merged skip, and an
    OMN-16788 ``unverifiable_cause`` skip are all skips the fix deliberately
    does not touch.
    """
    baseline = [
        _check(
            "dod-behavior",
            EnumEvidenceCheckStatus.VERIFIED,
            proof_class=EnumCheckProofClass.BEHAVIOR,
        ),
        _check("dod-surrogate", EnumEvidenceCheckStatus.NON_PROBATIVE),
    ]

    live_disabled = _check("dod-a::pr-live-state", EnumEvidenceCheckStatus.SKIPPED)
    asserted_non_merged = _check(
        "dod-b::pr-live-state", EnumEvidenceCheckStatus.SKIPPED
    )
    unverifiable = _check(
        "dod-c::pr-live-state",
        EnumEvidenceCheckStatus.SKIPPED,
        unverifiable_cause=(
            EnumEvidenceUnverifiableCause.CREDENTIAL_CANNOT_READ_BRANCH_PROTECTION
        ),
    )

    for extra in (live_disabled, asserted_non_merged, unverifiable):
        state = HandlerDodVerify()._handle_typed(_command(), [*baseline, extra])
        assert state.unbindable_overlay_count == 0
        assert state.total_checks == 3, extra.evidence_id
        assert state.verified_count + state.non_probative_count != state.total_checks, (
            f"{extra.evidence_id} must still break the flip equality"
        )


@pytest.mark.unit
def test_a_run_whose_only_entries_are_unbindable_overlays_fails_closed() -> None:
    """Excluding the overlay must not manufacture a green out of nothing.

    The OMN-15390 anti-laundering rule, applied to the new exclusion: with the
    overlay out of the denominator the run has ZERO verdict-bearing checks, and
    a zero denominator is a SKIP, never a VERIFIED.
    """
    state = HandlerDodVerify()._handle_typed(
        _command(),
        [
            _check(
                "dod-a::pr-live-state",
                EnumEvidenceCheckStatus.SKIPPED,
                unbindable_derived_overlay=True,
            ),
            _check(
                "dod-b::pr-live-state",
                EnumEvidenceCheckStatus.SKIPPED,
                unbindable_derived_overlay=True,
            ),
        ],
    )
    assert state.total_checks == 0
    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.verified_count == 0


@pytest.mark.unit
def test_a_real_skip_alongside_an_overlay_still_holds_the_verdict() -> None:
    """The overlay exclusion must not let a genuine skip pass as "all skipped".

    Fail-open trap: the overlay leaves ``total_checks`` but stays in the raw
    SKIPPED tally, so a naive ``skipped == total_checks`` comparison stops
    matching and the run falls through to VERIFIED with zero verified checks.
    """
    state = HandlerDodVerify()._handle_typed(
        _command(),
        [
            _check("dod-real", EnumEvidenceCheckStatus.SKIPPED),
            _check(
                "dod-real::pr-live-state",
                EnumEvidenceCheckStatus.SKIPPED,
                unbindable_derived_overlay=True,
            ),
        ],
    )
    assert state.total_checks == 1
    assert state.verified_count == 0
    assert state.status is EnumDodVerifyStatus.SKIPPED


def _isolate_occ_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point receipt resolution at ``tmp_path`` and nowhere else.

    Mirrors ``test_omn_15465_pr_live_state_carrier_misbind`` — this workspace
    exports ``CONTRACT_REPO_DIR`` / ``ONEX_CC_REPO_PATH``, and an ambient value
    redirects the lookup away from the fixture, making the assertion vacuous.
    """
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv("CONTRACT_REPO_DIR", raising=False)
    monkeypatch.delenv("ONEX_CC_REPO_PATH", raising=False)
    return tmp_path / "onex_change_control"


@pytest.mark.unit
def test_collector_marks_the_unbindable_overlay_it_derives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End of the chain: the producer sets the marker the counter reads.

    An item with a PASS receipt naming a ``pr_number`` that no receipt field
    pairs with a repo — the OMN-15382 F2c shape, and OMN-17313's actual one.
    """
    occ_root = _isolate_occ_root(tmp_path, monkeypatch)
    item_id = "dod-occ-diff-derived-behavior-proof"
    receipt_dir = occ_root / "drift" / "dod_receipts" / TICKET / item_id
    receipt_dir.mkdir(parents=True)
    (receipt_dir / "command.yaml").write_text(
        yaml.safe_dump({"status": "PASS", "pr_number": 2093}),
        encoding="utf-8",
    )

    collector = EvidenceCollector()
    results = collector._live_pr_checks_for_item(
        {"id": item_id, "description": "diff-derived behavior proof"},
        TICKET,
        None,
    )

    assert len(results) == 1
    overlay = results[0]
    assert overlay.evidence_id == f"{item_id}::pr-live-state"
    assert overlay.status is EnumEvidenceCheckStatus.SKIPPED
    assert overlay.unbindable_derived_overlay is True
    assert "NO_CONSISTENT_PR_BINDING" in (overlay.message or "")
