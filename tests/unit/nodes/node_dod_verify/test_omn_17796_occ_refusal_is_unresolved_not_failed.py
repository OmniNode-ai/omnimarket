# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17796 — the OCC governance-ref refusal is a REFUSAL, not a red verdict.

OMN-16787 made ``collect()`` refuse when the ``origin/dev`` worktree cannot be
materialised. That refusal is correct and stays. What was wrong is its
ENCODING: it returned one synthetic ``FAILED`` check, and
``HandlerDodVerify._handle_typed`` therefore took the ``elif failed > 0`` arm
and emitted a plain::

    ModelDodVerifyState(status=FAILED, total_checks=1, verified_count=0,
                        failed_count=1, unresolved_cause=None,
                        error_message=None)

— indistinguishable, to every consumer, from a ticket whose evidence was read
and found wanting. Nothing was read: the refusal happens BEFORE the contract
is loaded.

Measured 2026-09-03 (36 runs, 12 tickets x 3, 6-way parallel, ledger
``2026-09-03T17:59:57Z`` lane=``dod-verify-measurement``): 12 of 36 runs
collapsed to that shape, and all 12 produced the SAME
``verdict_content_sha256``
``65c7799a96523b41d91de31e5a19f7858370bc1a331b245cfdd946e6c63232c6`` across six
unrelated tickets (OMN-17323 / 17504 / 17519 / 17531 / 17533 / 17534). A
verdict that does not depend on the ticket is not a verdict about the ticket.

Downstream, the omnibase_infra autoclose sweep's ``_gap_shortfall``
(``handler_evidence_autoclose_sweep.py:527-571``) is a most-specific-first
branch table whose FIRST branch is ``if failed_count > 0``, so it posted
``"0/1 ACs verified, 1 failed - not all ACs are receipt-proven."`` — a false
accusation against a ticket, for the verifier's own git lock contention.

The fix routes both OCC governance-ref refusals through the type OMN-17022
already built for exactly this class (``EnumDodVerifyStatus.UNRESOLVED`` +
``EnumDodVerifyUnresolvedCause``), which OMN-17022 wired only to the per-item
PR/repo binding path.

RED-first, driving the real ``EvidenceCollector`` / ``HandlerDodVerify`` path
with real git subprocesses. The reproduction is the OMN-16787 one: an OCC clone
with no ``origin/dev`` ref makes ``git worktree add --detach origin/dev`` fail
deterministically, which is the same ``(None, None, ...)`` return the
production 300 s timeout produces.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumCheckProofClass,
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    EnumOccRefRefreshOutcome,
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    _ALLOW_STALE_OCC_REF_ENV,
    _GIT_OP_TIMEOUT_ENV,
    EvidenceCollector,
)

# The two refusal codes are spelled here as LITERALS rather than imported from
# the collector, deliberately, on two counts. They are the operator-facing
# contract — the head of the message a human reads and the string
# ``from_error_code`` maps — so a test that imported the constant would keep
# passing under a rename that broke both. And spelling them out keeps this
# module IMPORTABLE against the pre-fix product code, which is what makes the
# red a behavioural failure ("FAILED is not UNRESOLVED") rather than a
# collection error that proves nothing about behaviour.
_OCC_WORKTREE_UNAVAILABLE_CODE = "OCC_WORKTREE_UNAVAILABLE"
_OCC_REF_REFRESH_FAILED_CODE = "OCC_REF_REFRESH_FAILED"

# Two of the six unrelated tickets that produced the one identical verdict
# hash. Used verbatim so the ticket-independence assertion below is over the
# real shape rather than a synthetic pair.
_TICKET_A = "OMN-17519"
_TICKET_B = "OMN-17323"

#: The verdict hash all 12 collapsed runs shared. Recorded here as the fact the
#: fix answers, not as an input to any assertion.
_MEASURED_TICKET_INDEPENDENT_VERDICT_SHA = (
    "65c7799a96523b41d91de31e5a19f7858370bc1a331b245cfdd946e6c63232c6"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _occ_without_a_dev_ref(tmp_path: Path, *tickets: str) -> Path:
    """An OCC clone carrying the contracts on its WORKING TREE only.

    ``origin/dev`` does not exist, so the real ``git worktree add --detach
    --force <tmp> origin/dev`` fails — the same ``(None, None, ...)`` the
    production timeout returns, reached through the real code path rather than
    by mocking the method under test.

    The contracts ARE present on the working tree, which is what makes the
    negative assertions meaningful: a collector that silently fell back would
    find real evidence items and emit a real (mis-attributed) verdict.
    """
    occ = tmp_path / "onex_change_control"
    occ.mkdir()
    _git(occ, "init", "-q")
    _git(occ, "config", "user.email", "t@t.co")
    _git(occ, "config", "user.name", "t")
    _git(occ, "checkout", "-q", "-b", "main")
    contract_dir = occ / "contracts"
    contract_dir.mkdir()
    for ticket in tickets:
        (contract_dir / f"{ticket}.yaml").write_text(
            "schema_version: '1.0.0'\n"
            f"ticket_id: {ticket}\n"
            "dod_evidence:\n"
            "  - id: dod-working-tree-only\n"
            "    description: trivially true\n"
            "    checks:\n"
            "      - check_type: command\n"
            "        check_value: 'true'\n",
            encoding="utf-8",
        )
    _git(occ, "add", "-A")
    _git(occ, "commit", "-q", "-m", "main contracts")
    return occ


def _force_fetch_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the refresh leg succeed so the WORKTREE leg is the one under test.

    Without this the run short-circuits into the OMN-15454 fetch refusal and
    the worktree branch is never reached — the test would pass for the wrong
    reason.
    """

    def _fake_run_occ_fetch(
        self: EvidenceCollector, occ_path: Path, remote: str, branch: str
    ) -> tuple[EnumOccRefRefreshOutcome, str]:
        return EnumOccRefRefreshOutcome.FETCHED, ""

    monkeypatch.setattr(EvidenceCollector, "_run_occ_fetch", _fake_run_occ_fetch)


def _force_fetch_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the OMN-15454 twin: the fetch itself fails."""

    def _fake_run_occ_fetch(
        self: EvidenceCollector, occ_path: Path, remote: str, branch: str
    ) -> tuple[EnumOccRefRefreshOutcome, str]:
        return EnumOccRefRefreshOutcome.FETCH_FAILED, "simulated fetch failure"

    monkeypatch.setattr(EvidenceCollector, "_run_occ_fetch", _fake_run_occ_fetch)


def _arrange_occ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *tickets: str
) -> None:
    occ = _occ_without_a_dev_ref(tmp_path, *tickets)
    monkeypatch.setenv("ONEX_CC_REPO_PATH", str(occ))
    monkeypatch.setenv("OCC_GOVERNANCE_REF", "origin/dev")
    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.delenv(_ALLOW_STALE_OCC_REF_ENV, raising=False)


def _verify(ticket: str) -> ModelDodVerifyState:
    """Run the node the way the CLI does: typed command, collector path."""
    return HandlerDodVerify()._handle_typed(
        ModelDodVerifyStartCommand(ticket_id=ticket, correlation_id=uuid4())
    )


# ---------------------------------------------------------------------------
# AC2 — the verdict is UNRESOLVED with a typed cause, and carries no red
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_worktree_refusal_is_unresolved_not_a_substantive_red_ac2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run reached no verdict, so it must not report one.

    Pre-fix this asserted ``status is FAILED`` / ``failed_count == 1`` /
    ``unresolved_cause is None`` — the shape measured 12 times.
    """
    _arrange_occ(tmp_path, monkeypatch, _TICKET_A)
    _force_fetch_ok(monkeypatch)

    state = _verify(_TICKET_A)

    assert state.status is EnumDodVerifyStatus.UNRESOLVED
    assert state.unresolved_cause is (
        EnumDodVerifyUnresolvedCause.OCC_WORKTREE_UNAVAILABLE
    )
    # Zero evidence checks executed, so zero were found wanting. This is the
    # single field the consumer's gap-sentence branch table keys on first.
    assert state.failed_count == 0
    # The refusal still OCCUPIES the verdict-bearing denominator: that is what
    # keeps the OMN-16821 equality leg (verified + non_probative == total)
    # false, so removing it would trade one blocker away for nothing.
    assert state.total_checks == 1
    assert state.verified_count == 0
    assert state.non_probative_count == 0
    assert state.behavior_proving_count == 0
    # The refusal record itself is still in the receipt, verbatim.
    assert len(state.checks) == 1
    assert state.checks[0].evidence_id == "occ_worktree_unavailable"
    assert _OCC_WORKTREE_UNAVAILABLE_CODE in (state.checks[0].message or "")
    # The working-tree contract was NOT silently substituted (OMN-16787's
    # decisive negative, re-pinned here because this ticket changes the arm
    # that reports it).
    assert not any(r.evidence_id == "dod-working-tree-only" for r in state.checks)


@pytest.mark.unit
def test_unresolved_message_names_the_occ_remedy_not_the_pr_binding_one_ac2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remedy printed must be the one that applies to THIS cause.

    OMN-17022's ``VERIFICATION_UNRESOLVED`` text is hardcoded to the PR/repo
    binding remedy ("Bind REPO/PR_NUMBER..."), and it also asserts "a retry
    reproduces it exactly" — both false for a contention-driven git ceiling
    trip, which is precisely the case where another attempt is the right move.
    """
    _arrange_occ(tmp_path, monkeypatch, _TICKET_A)
    _force_fetch_ok(monkeypatch)

    message = _verify(_TICKET_A).error_message or ""

    assert message.startswith(
        f"VERIFICATION_UNRESOLVED: "
        f"{EnumDodVerifyUnresolvedCause.OCC_WORKTREE_UNAVAILABLE.value}"
    )
    # Names the ref, the ceiling override, and the escape hatch — an operator
    # must not have to read the source to act.
    assert "origin/dev" in message
    assert _GIT_OP_TIMEOUT_ENV in message
    assert _ALLOW_STALE_OCC_REF_ENV in message
    # The PR/repo-binding remedy is the WRONG instruction here and must not
    # appear.
    assert "PR_NUMBER" not in message
    assert "autobind" not in message


# ---------------------------------------------------------------------------
# AC1 — typed provenance, not a grepped message
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_collector_exposes_the_cause_as_typed_provenance_ac1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler must read a typed code, never parse the rendered message."""
    _arrange_occ(tmp_path, monkeypatch, _TICKET_A)
    _force_fetch_ok(monkeypatch)

    collector = EvidenceCollector()
    results = collector.collect(_TICKET_A)

    assert collector.occ_ref_failure_code == _OCC_WORKTREE_UNAVAILABLE_CODE
    assert collector.occ_ref_failure_cause is (
        EnumDodVerifyUnresolvedCause.OCC_WORKTREE_UNAVAILABLE
    )
    # The per-item PR/repo channel is untouched: the two scopes stay separable.
    assert collector.lookup_failure_cause is None
    # The emitted message's head resolves through the EXISTING taxonomy mapper,
    # so the code and the operator-facing string cannot drift apart.
    assert (
        EnumDodVerifyUnresolvedCause.from_error_code(results[0].message or "")
        is EnumDodVerifyUnresolvedCause.OCC_WORKTREE_UNAVAILABLE
    )


@pytest.mark.unit
def test_fetch_refusal_twin_is_unresolved_on_the_same_terms_ac1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-15454's fetch refusal is the same class and gets the same encoding.

    It sits 30 lines above the worktree refusal, returns the same single
    synthetic check, and produced the same false accusation. Leaving it behind
    would leave an identical armed path.
    """
    _arrange_occ(tmp_path, monkeypatch, _TICKET_A)
    _force_fetch_failed(monkeypatch)

    state = _verify(_TICKET_A)

    assert state.status is EnumDodVerifyStatus.UNRESOLVED
    assert state.unresolved_cause is (
        EnumDodVerifyUnresolvedCause.OCC_REF_REFRESH_FAILED
    )
    assert state.failed_count == 0
    assert state.checks[0].evidence_id == "occ_ref_refresh"
    assert _OCC_REF_REFRESH_FAILED_CODE in (state.checks[0].message or "")


@pytest.mark.unit
def test_both_occ_causes_are_retry_eligible_and_the_binding_ones_are_not_ac1() -> None:
    """Retry policy is encoded on the member, per OMN-17022's own rule.

    An OCC refusal is a statement about the verifier's host under load — the
    collector has already spent its one in-run retry, so the next attempt has
    to be a scheduled one. A binding/credential defect reproduces exactly and
    stays refused.
    """
    assert EnumDodVerifyUnresolvedCause.OCC_WORKTREE_UNAVAILABLE.retry_eligible
    assert EnumDodVerifyUnresolvedCause.OCC_REF_REFRESH_FAILED.retry_eligible
    assert not EnumDodVerifyUnresolvedCause.PR_LOOKUP_FAILED.retry_eligible
    assert not EnumDodVerifyUnresolvedCause.UNKNOWN.retry_eligible


# ---------------------------------------------------------------------------
# AC3 — the OCC arm is run-wide, so it fires unconditionally
# ---------------------------------------------------------------------------


class _StubCollector:
    """A collector that returns a verified check ALONGSIDE the refusal.

    ``collect()`` cannot produce this today — it returns early with the single
    refusal — which is exactly why the guard has to be pinned here rather than
    inferred from the current call shape. If a later refactor ever moves the
    refusal alongside real results, the handler must still refuse.
    """

    occ_refresh_outcome = EnumOccRefRefreshOutcome.FETCHED
    occ_governance_ref = "origin/dev"
    occ_resolved_sha: str | None = None
    lookup_failure_code: str | None = None
    lookup_failure_cause: EnumDodVerifyUnresolvedCause | None = None

    def __init__(self, results: list[ModelEvidenceCheckResult], code: str) -> None:
        self._results = results
        self._code = code

    def collect(
        self, ticket_id: str, contract_path: str | None = None
    ) -> list[ModelEvidenceCheckResult]:
        return self._results

    @property
    def occ_ref_failure_code(self) -> str | None:
        return self._code

    @property
    def occ_ref_failure_cause(self) -> EnumDodVerifyUnresolvedCause | None:
        return EnumDodVerifyUnresolvedCause.from_error_code(self._code)


@pytest.mark.unit
def test_occ_arm_fires_beside_a_verified_behavior_check_ac3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run-wide refusal is not defeated by a sibling that happened to pass.

    The per-item PR-binding arm carries a ``verified == 0`` guard so a real red
    beside it is not stolen. The governance ref is different in kind: it is
    resolved BEFORE the contract loads, so if it failed, nothing in the run is
    attributable to the ref the run reports — including the sibling.
    """
    stub = _StubCollector(
        [
            ModelEvidenceCheckResult(
                evidence_id="dod-behaviour-proof",
                description="a real, passing, behaviour-proving check",
                status=EnumEvidenceCheckStatus.VERIFIED,
                proof_class=EnumCheckProofClass.BEHAVIOR,
            ),
            ModelEvidenceCheckResult(
                evidence_id="occ_worktree_unavailable",
                description="OCC worktree could not be materialised",
                status=EnumEvidenceCheckStatus.SKIPPED,
                message=f"{_OCC_WORKTREE_UNAVAILABLE_CODE}: simulated",
            ),
        ],
        _OCC_WORKTREE_UNAVAILABLE_CODE,
    )
    monkeypatch.setattr(HandlerDodVerify, "_make_collector", staticmethod(lambda: stub))

    state = _verify(_TICKET_A)

    # AC3's adversarial statement first: the passing sibling did NOT carry the
    # run to a verified verdict. Then the positive one, which is stronger.
    assert state.status is not EnumDodVerifyStatus.VERIFIED
    assert state.status is EnumDodVerifyStatus.UNRESOLVED
    assert state.unresolved_cause is (
        EnumDodVerifyUnresolvedCause.OCC_WORKTREE_UNAVAILABLE
    )
    # The sibling's own tallies are reported honestly; what changes is the
    # VERDICT, which stays unreached.
    assert state.verified_count == 1
    assert state.behavior_proving_count == 1
    assert state.error_message is not None


# ---------------------------------------------------------------------------
# AC4 — non-widening, proven conjunct by conjunct
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_flip_predicate_still_refuses_on_three_independent_legs_ac4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OMN-16821 autoclose predicate must refuse, and not by one leg only.

    Consumer predicate (omnibase_infra
    ``handler_evidence_autoclose_sweep.py:1475-1481``)::

        verify_status == "verified"
        and total_checks > 0
        and failed_count == 0
        and verified_count > 0
        and verified_count + non_probative_count == total_checks
        # then, separately: behavior_proving_count > 0

    ``failed_count == 0`` is now satisfied — honestly, because nothing failed —
    so each remaining leg is asserted on its own rather than trusting the
    conjunction.
    """
    _arrange_occ(tmp_path, monkeypatch, _TICKET_A)
    _force_fetch_ok(monkeypatch)

    state = _verify(_TICKET_A)

    # Leg 1 — dod_verify's own terminal status is not "verified".
    assert state.status.value != "verified"
    # Leg 2 — nothing verified.
    assert not state.verified_count > 0
    # Leg 3 — the equality the OMN-16821 numerator has to satisfy.
    assert state.verified_count + state.non_probative_count != state.total_checks
    # Leg 4 — the OMN-15911 behaviour conjunct.
    assert not state.behavior_proving_count > 0
    # And the CLI-visible fail-closed signal: RuntimeLocal._classify_result
    # reads a populated error_message as an unambiguous failure, so the skill
    # still exits non-zero.
    assert state.error_message is not None
    # The refusal is NOT laundered into either exemption channel.
    assert state.non_probative_count == 0
    assert state.unbindable_overlay_count == 0
    assert all(not c.unbindable_derived_overlay for c in state.checks)


@pytest.mark.unit
def test_refusal_never_reports_verified_for_any_ticket_ac4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one assertion that must hold no matter which arm is taken."""
    _arrange_occ(tmp_path, monkeypatch, _TICKET_A, _TICKET_B)
    _force_fetch_ok(monkeypatch)

    for ticket in (_TICKET_A, _TICKET_B):
        assert _verify(ticket).status is not EnumDodVerifyStatus.VERIFIED


# ---------------------------------------------------------------------------
# AC5 / AC6 — the measured shape, and what the consumer can now say about it
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ticket_independent_refusal_is_typed_as_such_ac6(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two unrelated tickets, one refusal — and it now SAYS it is one.

    The measured defect was that this record was byte-identical across six
    unrelated tickets while claiming to be a per-ticket red
    (``verdict_content_sha256`` ``65c7799...``). The refusal is still
    ticket-independent — it is a fact about the host, not the ticket — but it
    is now typed as un-reached, so no consumer can read it as a verdict.
    """
    _arrange_occ(tmp_path, monkeypatch, _TICKET_A, _TICKET_B)
    _force_fetch_ok(monkeypatch)

    a = _verify(_TICKET_A)
    b = _verify(_TICKET_B)

    assert len(_MEASURED_TICKET_INDEPENDENT_VERDICT_SHA) == 64
    assert a.ticket_id != b.ticket_id
    for state in (a, b):
        assert state.status is EnumDodVerifyStatus.UNRESOLVED
        assert state.unresolved_cause is (
            EnumDodVerifyUnresolvedCause.OCC_WORKTREE_UNAVAILABLE
        )
        assert state.failed_count == 0


@pytest.mark.unit
def test_payload_selects_the_true_consumer_gap_sentence_ac5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the two fields the consumer's branch table reads, in its own order.

    ``_gap_shortfall`` (omnibase_infra ``handler_evidence_autoclose_sweep.py:
    544-557``) is most-specific-first::

        if failed_count > 0:                   -> "not all ACs are receipt-proven."
        if verified_count == 0 and non_probative_count > 0:
                                               -> the OMN-15391 sentence
        if verify_status != "verified":        -> "dod_verify's own terminal
                                                   status is <status>, not
                                                   'verified'."

    This asserts the payload facts that make branch 3 the selected one. The
    consumer is NOT modified by this ticket; the live readback against the
    unmodified function is recorded on OMN-17796.
    """
    _arrange_occ(tmp_path, monkeypatch, _TICKET_A)
    _force_fetch_ok(monkeypatch)

    state = _verify(_TICKET_A)

    # Branch 1 cannot fire: nothing failed.
    assert state.failed_count == 0
    # Branch 2 cannot fire: nothing was non-probative either.
    assert not (state.verified_count == 0 and state.non_probative_count > 0)
    # Branch 3 fires, and the status it will name is the typed one.
    assert state.status.value != "verified"
    assert state.status.value == "unresolved"


@pytest.mark.unit
def test_each_refusal_code_maps_to_its_own_member_with_no_second_table_ac1() -> None:
    """The code the collector emits and the member the handler types on are one.

    AC1 requires the two new members to be *named so that the existing*
    ``from_error_code`` *head-mapping resolves them without a second mapping
    table*. That is not a style note — it is the property that stops the string
    an operator reads and the type a consumer branches on from drifting apart,
    and it is directly checkable: ``from_error_code`` upper-cases the head and
    looks it up as a member VALUE, so the invariant holds iff each code equals
    its member's value upper-cased.

    Asserted against the literal codes, so a rename that moved the collector
    constant but not the operator-facing string is still caught. On the pre-fix
    taxonomy both codes fall through to ``UNKNOWN`` — the fail-closed default
    for a code it has never seen — which is exactly the gap this ticket closes.
    """
    for code, expected in (
        (
            _OCC_WORKTREE_UNAVAILABLE_CODE,
            EnumDodVerifyUnresolvedCause.OCC_WORKTREE_UNAVAILABLE,
        ),
        (
            _OCC_REF_REFRESH_FAILED_CODE,
            EnumDodVerifyUnresolvedCause.OCC_REF_REFRESH_FAILED,
        ),
    ):
        assert EnumDodVerifyUnresolvedCause.from_error_code(code) is expected
        # The naming invariant itself, stated rather than implied.
        assert expected.value.upper() == code
        # And the rendered "CODE: detail" form the collector actually stores
        # resolves identically, so no producer has to strip its own detail.
        assert (
            EnumDodVerifyUnresolvedCause.from_error_code(f"{code}: some detail")
            is expected
        )
