# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17795: the verifier's own per-check budget is not a verdict about the work.

The third independent unsoundness in the LOCAL ``dod_verify`` path, alongside
its OMN-16846 siblings ``PRODUCT_CLONE_STALE`` and ``GATE_VENV_IMPURE``, and
the one that made the verifier non-reproducible.

**What was measured** (36 runs, 12 tickets x 3 back-to-back, 6-way parallel,
nothing changed between runs; ledger ``2026-09-03T17:59:57Z``): 8 of 12 tickets
produced a different ``verdict_content_sha256`` across three consecutive runs.
OMN-16803 flipped verdict STATUS ``failed`` -> ``skipped`` -> ``failed``.
OMN-16984 produced three distinct hashes with verified ``10->9->9``, failed
``1->1->2``, behavior_proving ``1->0->0``. Every observed delta localised to a
single check -- the OMN-16434 auto-minted ``dod-occ-diff-derived-behavior-proof``
-- racing between three environmental terminal states.

**Why it flipped.** Two of those three states were recorded as a typed
environmental non-result (SKIPPED + ``unverifiable_cause``); the third, a trip
of the collector's own per-check ceiling, was recorded as a substantive FAILED.
``HandlerDodVerify._handle_typed`` orders ``failed > 0`` ahead of its
``unverifiable`` arm, so which of the three won the race decided whether the
ticket read ``failed`` or ``skipped``. That is the flip, in full.

**Why FAILED was the wrong record.** The ceiling belongs to the verifier, not
to the product. The one time that check verified in 36 runs it read
``OK (19850ms): 16 passed in 1.55s`` -- 18.3s of pytest import/collection
overhead against a 30s ceiling, and 1.55s of actual product work. The identical
command tripped the ceiling on the next two runs. Recording that FAILED asserts
a defect the run never looked for: verbatim the argument already accepted at
``GATE_VENV_IMPURE`` and ``PRODUCT_CLONE_STALE``.

**What is NOT relaxed.** A tripped check still blocks, on every conjunct
separately asserted below: the overall verdict is never VERIFIED, the check
stays in the verdict-bearing denominator, it is not ``non_probative``, it is
not ``behavior_proving``, and the OMN-16821 autoclose flip predicate still
refuses. The change is in HOW the block is recorded, never WHETHER it blocks --
the OMN-16788 shape, one axis over.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

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
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    _CHECK_TIMEOUT_ENV,
    _DEFAULT_CHECK_TIMEOUT_S,
    EvidenceCollector,
    _check_timeout_s,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures — the real shape, not a hand-built one
# ---------------------------------------------------------------------------


def _item(
    command: str, *, item_id: str = "dod-occ-diff-derived-behavior-proof"
) -> dict[str, object]:
    """The OMN-16434 auto-minted behaviour-proof item, in its real shape.

    ``item_id`` defaults to the id every OCC companion carries since OMN-16434,
    because that is the single item every observed verdict delta localised to.
    """
    return {
        "id": item_id,
        "description": "diff-derived behavior proof",
        "checks": [{"check_type": "test_passes", "command": command}],
    }


@pytest.fixture
def collector(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> EvidenceCollector:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv(_CHECK_TIMEOUT_ENV, raising=False)
    # These items bind to no PR; the OMN-14207 live check would shell out to
    # `gh` for a merge state irrelevant to a budget trip.
    monkeypatch.setenv("DOD_VERIFY_LIVE_PR_CHECK", "0")
    return EvidenceCollector()


def _result(
    status: EnumEvidenceCheckStatus,
    *,
    cause: EnumEvidenceUnverifiableCause | None = None,
    proof_class: EnumCheckProofClass = EnumCheckProofClass.BEHAVIOR,
    evidence_id: str = "dod-occ-diff-derived-behavior-proof",
) -> ModelEvidenceCheckResult:
    return ModelEvidenceCheckResult(
        evidence_id=evidence_id,
        description="diff-derived behavior proof",
        status=status,
        unverifiable_cause=cause,
        proof_class=proof_class,
    )


def _verdict(checks: list[ModelEvidenceCheckResult]) -> ModelDodVerifyState:
    """Drive the handler's typed entry point over caller-supplied results.

    The documented path for asserting the handler's own arithmetic without
    re-executing a subprocess whose timing is the very thing under test.
    """
    return HandlerDodVerify()._handle_typed(
        ModelDodVerifyStartCommand(ticket_id="OMN-16803"),
        evidence_results=checks,
    )


# ---------------------------------------------------------------------------
# RED-first premise — the race, stated without the fix
# ---------------------------------------------------------------------------


def test_the_same_command_trips_or_completes_purely_on_the_budget(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect's mechanism, asserted directly before anything is claimed.

    One command, one tree, one process. The only variable is the ceiling the
    verifier holds -- which is exactly what host load varies in effect. If this
    does not diverge, the rest of the file is testing a supposed race.
    """
    monkeypatch.setenv(_CHECK_TIMEOUT_ENV, "60")
    completed = collector._run_command_check(_item("sleep 0.4")["checks"][0], "OMN-1")  # type: ignore[index]

    monkeypatch.setenv(_CHECK_TIMEOUT_ENV, "0.2")
    tripped = collector._run_command_check(_item("sleep 0.4")["checks"][0], "OMN-1")  # type: ignore[index]

    assert completed[0] is True
    assert tripped[0] is False


# ---------------------------------------------------------------------------
# AC1 — the ceiling is declared, overridable, and read per call
# ---------------------------------------------------------------------------


def test_the_ceiling_has_a_named_operator_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_CHECK_TIMEOUT_ENV, "125")
    assert _check_timeout_s() == 125.0


def test_the_ceiling_is_read_per_call_not_captured_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator must be able to raise it without reloading the module.

    Mirrors ``_git_op_timeout_s``'s own contract, which this ceiling was the
    only sibling not to have.
    """
    monkeypatch.setenv(_CHECK_TIMEOUT_ENV, "11")
    first = _check_timeout_s()
    monkeypatch.setenv(_CHECK_TIMEOUT_ENV, "22")
    assert (first, _check_timeout_s()) == (11.0, 22.0)


def test_an_unset_override_resolves_to_the_declared_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_CHECK_TIMEOUT_ENV, raising=False)
    assert _check_timeout_s() == float(_DEFAULT_CHECK_TIMEOUT_S)


@pytest.mark.parametrize("raw", ["not-a-number", "", "   ", "-5", "-0.1"])
def test_a_malformed_or_negative_override_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Never unbounded.

    An unbounded per-check subprocess is the failure mode the ceiling exists
    to prevent, so a value that cannot be honoured resolves to the default
    rather than disabling the bound -- the ``_git_op_timeout_s`` rule.
    """
    monkeypatch.setenv(_CHECK_TIMEOUT_ENV, raw)
    assert _check_timeout_s() == float(_DEFAULT_CHECK_TIMEOUT_S)


def test_a_malformed_override_is_warned_about_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(_CHECK_TIMEOUT_ENV, "banana")
    with caplog.at_level(logging.WARNING):
        _check_timeout_s()
    assert _CHECK_TIMEOUT_ENV in caplog.text


# ---------------------------------------------------------------------------
# AC2 — a budget trip is a typed environmental non-result, positively identified
# ---------------------------------------------------------------------------


def test_a_budget_trip_is_skipped_with_a_named_cause_not_failed(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect, fixed: the ceiling is the verifier's, so it is not a red."""
    monkeypatch.setenv(_CHECK_TIMEOUT_ENV, "0.2")

    result = collector._check_evidence_item(_item("sleep 5"), "OMN-16803")

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert (
        result.unverifiable_cause is EnumEvidenceUnverifiableCause.CHECK_BUDGET_EXCEEDED
    )


def test_the_refusal_names_the_override_that_raises_the_ceiling(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An actionable refusal, as ``OCC_WORKTREE_UNAVAILABLE`` already is."""
    monkeypatch.setenv(_CHECK_TIMEOUT_ENV, "0.2")

    result = collector._check_evidence_item(_item("sleep 5"), "OMN-16803")

    assert result.message is not None
    assert "CHECK_BUDGET_EXCEEDED" in result.message
    assert _CHECK_TIMEOUT_ENV in result.message


def test_a_genuine_non_zero_exit_inside_the_budget_is_still_a_substantive_red(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The discriminator is the ceiling, never a bare non-zero exit.

    This is the whole of what keeps the change from being a relaxation: a
    command that RAN and reported a defect is untouched.
    """
    monkeypatch.setenv(_CHECK_TIMEOUT_ENV, "60")

    result = collector._check_evidence_item(_item("exit 1"), "OMN-16803")

    assert result.status is EnumEvidenceCheckStatus.FAILED
    assert result.unverifiable_cause is None


def test_the_cause_is_not_inferred_from_message_text(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command that PRINTS the banner and exits non-zero is a real red.

    The OMN-16788 rule -- consumers branch on the typed field, and the
    producer must not mint that field by grepping a subprocess's own output,
    which any product under test can forge.
    """
    monkeypatch.setenv(_CHECK_TIMEOUT_ENV, "60")

    result = collector._check_evidence_item(
        _item("echo 'CHECK_BUDGET_EXCEEDED: Timed out after 30s' >&2; exit 1"),
        "OMN-16803",
    )

    assert result.status is EnumEvidenceCheckStatus.FAILED
    assert result.unverifiable_cause is None


def test_the_command_is_not_left_running_past_the_ceiling(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is real: refusing must not mean waiting for it anyway."""
    monkeypatch.setenv(_CHECK_TIMEOUT_ENV, "0.3")

    start = time.monotonic()
    result = collector._check_evidence_item(_item("sleep 30"), "OMN-16803")
    elapsed = time.monotonic() - start

    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert elapsed < 10


# ---------------------------------------------------------------------------
# AC3 — non-widening, one conjunct at a time
# ---------------------------------------------------------------------------


def test_a_budget_trip_never_reaches_a_verified_verdict() -> None:
    state = _verdict(
        [
            _result(EnumEvidenceCheckStatus.VERIFIED),
            _result(
                EnumEvidenceCheckStatus.SKIPPED,
                cause=EnumEvidenceUnverifiableCause.CHECK_BUDGET_EXCEEDED,
                evidence_id="dod-002",
            ),
        ]
    )
    assert state.status is not EnumDodVerifyStatus.VERIFIED


def test_a_budget_trip_stays_in_the_verdict_bearing_denominator() -> None:
    """It is NOT an OMN-17323 unbindable overlay.

    That exclusion is for an entry the verifier auto-derived and could never
    bind. A budget trip is a real, ticket-declared criterion that was simply
    not given time -- excluding it from the denominator would report the
    ticket as fully proven on a check nobody ran.
    """
    state = _verdict(
        [
            _result(EnumEvidenceCheckStatus.VERIFIED),
            _result(
                EnumEvidenceCheckStatus.SKIPPED,
                cause=EnumEvidenceUnverifiableCause.CHECK_BUDGET_EXCEEDED,
                evidence_id="dod-002",
            ),
        ]
    )
    assert state.total_checks == 2
    assert all(not c.unbindable_derived_overlay for c in state.checks)


def test_a_budget_trip_is_not_counted_non_probative() -> None:
    state = _verdict(
        [
            _result(
                EnumEvidenceCheckStatus.SKIPPED,
                cause=EnumEvidenceUnverifiableCause.CHECK_BUDGET_EXCEEDED,
            )
        ]
    )
    assert state.non_probative_count == 0


def test_a_budget_trip_is_not_counted_behavior_proving() -> None:
    """Even though the item IS behaviour-class -- it did not execute.

    ``behavior_proving`` is the conjunction VERIFIED and BEHAVIOR. A trip
    keeps the class and loses the status, which is the honest record.
    """
    state = _verdict(
        [
            _result(
                EnumEvidenceCheckStatus.SKIPPED,
                cause=EnumEvidenceUnverifiableCause.CHECK_BUDGET_EXCEEDED,
                proof_class=EnumCheckProofClass.BEHAVIOR,
            )
        ]
    )
    assert state.behavior_proving_count == 0


def test_the_autoclose_flip_predicate_still_refuses_a_budget_trip() -> None:
    """The adversarial case: the ONLY thing standing between this contract and
    a Done-flip is the tripped check.

    The OMN-16821 predicate is ``verified + non_probative == total_checks AND
    behavior_proving > 0``. Asserted here as the consuming lane evaluates it,
    so the guarantee is about the live predicate rather than about a status
    label that a future consumer might read differently.
    """
    state = _verdict(
        [
            _result(EnumEvidenceCheckStatus.VERIFIED, evidence_id="dod-001"),
            _result(
                EnumEvidenceCheckStatus.NON_PROBATIVE,
                proof_class=EnumCheckProofClass.SURROGATE,
                evidence_id="dod-002",
            ),
            _result(
                EnumEvidenceCheckStatus.SKIPPED,
                cause=EnumEvidenceUnverifiableCause.CHECK_BUDGET_EXCEEDED,
                evidence_id="dod-occ-diff-derived-behavior-proof",
            ),
        ]
    )

    would_flip = (
        state.verified_count + state.non_probative_count == state.total_checks
        and state.behavior_proving_count > 0
    )
    assert would_flip is False


def test_the_run_still_reports_a_blocking_error_message() -> None:
    """A SKIPPED verdict must not read as healthy to a caller that only reads
    ``error_message`` -- the OMN-15380 fail-closed rule."""
    state = _verdict(
        [
            _result(
                EnumEvidenceCheckStatus.SKIPPED,
                cause=EnumEvidenceUnverifiableCause.CHECK_BUDGET_EXCEEDED,
            )
        ]
    )
    assert state.error_message is not None


# ---------------------------------------------------------------------------
# AC4 — determinism across the three environmental mechanisms
# ---------------------------------------------------------------------------


_ENVIRONMENTAL_CAUSES = [
    EnumEvidenceUnverifiableCause.CHECK_BUDGET_EXCEEDED,
    EnumEvidenceUnverifiableCause.PRODUCT_CLONE_STALE,
    EnumEvidenceUnverifiableCause.GATE_VENV_IMPURE,
]


@pytest.mark.parametrize("cause", _ENVIRONMENTAL_CAUSES)
def test_every_environmental_mechanism_yields_the_same_verdict_status(
    cause: EnumEvidenceUnverifiableCause,
) -> None:
    """The OMN-16803 flip, closed.

    The real contract shape it flipped on: a verified sibling, a non-probative
    surrogate, and the one OMN-16434 behaviour item that raced. Whichever
    mechanism wins the race, the ticket reads the same -- so a verdict can no
    longer depend on host load.
    """
    state = _verdict(
        [
            _result(EnumEvidenceCheckStatus.VERIFIED, evidence_id="dod-001"),
            _result(
                EnumEvidenceCheckStatus.NON_PROBATIVE,
                proof_class=EnumCheckProofClass.SURROGATE,
                evidence_id="dod-002",
            ),
            _result(EnumEvidenceCheckStatus.SKIPPED, cause=cause),
        ]
    )
    assert state.status is EnumDodVerifyStatus.SKIPPED


def test_the_three_mechanisms_agree_on_every_counted_axis() -> None:
    """Not just the status label -- the whole tally a consumer reads.

    ``verdict_content_sha256`` is computed over these counts, so agreement
    here is what makes the digest a usable hand-comparability anchor.
    """
    tallies = {
        (
            state.total_checks,
            state.verified_count,
            state.failed_count,
            state.skipped_count,
            state.non_probative_count,
            state.behavior_proving_count,
        )
        for state in (
            _verdict(
                [
                    _result(EnumEvidenceCheckStatus.VERIFIED, evidence_id="dod-001"),
                    _result(EnumEvidenceCheckStatus.SKIPPED, cause=cause),
                ]
            )
            for cause in _ENVIRONMENTAL_CAUSES
        )
    }
    assert len(tallies) == 1


def test_a_collector_produced_budget_trip_drives_the_handler_to_skipped(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end across the seam: no hand-built result in this one.

    The two halves of the fix are in different objects; this is the only test
    that proves they meet.
    """
    monkeypatch.setenv(_CHECK_TIMEOUT_ENV, "0.2")
    tripped = collector._check_evidence_item(_item("sleep 5"), "OMN-16803")

    state = _verdict(
        [_result(EnumEvidenceCheckStatus.VERIFIED, evidence_id="a"), tripped]
    )

    assert state.status is EnumDodVerifyStatus.SKIPPED
    assert state.failed_count == 0


# ---------------------------------------------------------------------------
# AC5 — the overturned docstring clause is corrected, not silently contradicted
# ---------------------------------------------------------------------------


_OVERTURNED_CLAUSE = "A timeout, a 5xx, or an OSError stays a substantive"


def test_the_overturned_clause_survives_only_as_a_quotation_of_itself() -> None:
    """The OMN-16788 clause this ticket overturns, pinned so it cannot revert.

    It read: "A timeout, a 5xx, or an OSError stays a substantive fail-closed
    FAILURE". Written when the enum held only the two credential renderings;
    OMN-16846 then added two non-credential causes without revisiting it. A
    docstring that contradicts the code it documents is how the next reader
    re-litigates a settled decision.

    Deleting it outright would be the other way to fail: a future reader would
    have no record that the question was asked and answered, and would be free
    to re-derive the old rule. So AC5 requires the clause be QUOTED and
    overturned, which is asserted here positionally — the overturn must
    introduce the quote, not trail it.
    """
    doc = EnumEvidenceUnverifiableCause.__doc__ or ""
    assert _OVERTURNED_CLAUSE in doc, "the overturned clause must stay on the record"
    assert doc.index("OMN-17795 overturns") < doc.index(_OVERTURNED_CLAUSE)


def test_the_enum_records_why_the_feared_laundering_channel_stays_shut() -> None:
    """AC5 asks for the answer on the record, not merely the reversal."""
    doc = EnumEvidenceUnverifiableCause.__doc__ or ""
    assert "OMN-17795" in doc
