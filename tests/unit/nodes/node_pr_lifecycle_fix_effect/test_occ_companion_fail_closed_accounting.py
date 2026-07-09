# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14173 — fail-closed prs_fixed accounting for the OCC autobind arm.

Regression coverage for the ``merge_sweep --fix-only`` false-success: on
omnimarket #1651/#1652 the sweep reported ``prs_fixed=2`` while authoring ZERO
OCC companions. Two defects combined:

  1. ROUTING — an ``occ-preflight / eligibility`` failure (with a ticket) must
     reach the ``receipt_evidence_source_autobind`` arm (already landed in the
     OMN-13990 follow-up; re-asserted here as a witness).
  2. ACCOUNTING — the autobind arm reported ``fix_applied=True`` whenever the
     adapter call returned, so ``prs_fixed`` counted PRs whose companion was
     never pushed. It must now count a PR ONLY after an independent read-back
     verifies the pushed OCC companion + Evidence-Source patch.

These tests are hermetic — the OCC companion verifier is a fake; no gh/network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_result import (
    ModelOccCompanionVerification,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    HandlerPrLifecycleOrchestrator,
    _block_reason_for_fix,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumPrCategory,
    TriageRecord,
)

_OCC_PREFLIGHT = "occ-preflight / eligibility"


class _RecordingAutobindAdapter:
    """Records autobind calls and returns a success string WITHOUT pushing.

    This is exactly the shape that produced the false-success: the call returns
    cleanly (so ``fix_applied=True``) but no companion is actually authored.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None]] = []

    async def autobind_evidence_source(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> str:
        self.calls.append((repo, pr_number, ticket_id))
        return f"[stub] would autobind {repo}#{pr_number}"


class _FakeVerifier:
    """Fake OCC companion verifier returning a fixed verdict; records calls."""

    def __init__(self, *, verified: bool) -> None:
        self._verified = verified
        self.calls: list[tuple[str, int, str | None]] = []

    async def verify_companion(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> ModelOccCompanionVerification:
        self.calls.append((repo, pr_number, ticket_id))
        return ModelOccCompanionVerification(
            verified=self._verified,
            occ_pr_number=4242 if self._verified else None,
            evidence_source_present=self._verified,
            occ_pr_open=self._verified,
            branch_exists=self._verified,
            detail="verified" if self._verified else "companion not bound",
        )


def _occ_preflight_pr() -> TriageRecord:
    """A green-except-OCC-companion PR: only occ-preflight fails, ticket present."""
    return TriageRecord(
        pr_number=1651,
        repo="OmniNode-ai/omnimarket",
        category=EnumPrCategory.RED,
        ticket_ids=("OMN-14173",),
        failed_check_names=(_OCC_PREFLIGHT,),
    )


def _make_orchestrator(
    fix_handler: HandlerPrLifecycleFix,
) -> HandlerPrLifecycleOrchestrator:
    return HandlerPrLifecycleOrchestrator(
        event_bus=MagicMock(spec=ProtocolEventBusPublisher),
        fix=fix_handler,
    )


async def _dispatch_one(orch: HandlerPrLifecycleOrchestrator, pr: TriageRecord) -> int:
    """Run the fix dispatch for a single PR and return summed prs_dispatched."""
    results = await orch._dispatch_fix_parallel(
        fix_prs=(pr,),
        correlation_id=uuid4(),
        dry_run=False,
        max_parallel=1,
        enable_admin_merge_fallback=False,
        admin_fallback_threshold_minutes=30,
    )
    return sum(r.prs_dispatched for r in results)


@pytest.mark.unit
class TestOccPreflightRoutesToAutobind:
    """Defect 1 — routing witness."""

    def test_occ_preflight_only_with_ticket_routes_to_autobind(self) -> None:
        assert _block_reason_for_fix(_occ_preflight_pr()) == (
            EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND
        )

    async def test_occ_preflight_pr_reaches_autobind_adapter_via_dispatch(self) -> None:
        adapter = _RecordingAutobindAdapter()
        fix = HandlerPrLifecycleFix(
            occ_autobind_adapter=adapter,
            occ_companion_verifier=_FakeVerifier(verified=True),
        )
        orch = _make_orchestrator(fix)

        await _dispatch_one(orch, _occ_preflight_pr())

        assert adapter.calls == [("OmniNode-ai/omnimarket", 1651, "OMN-14173")]


@pytest.mark.unit
class TestFailClosedAccounting:
    """Defect 2 — prs_fixed is gated on a verified pushed companion."""

    async def test_unverified_companion_is_not_counted(self) -> None:
        """The false-success shape: adapter returns cleanly, nothing pushed.

        fix_applied is True (the route ran) but the companion is NOT verified,
        so the orchestrator must count the PR as skipped, never as fixed.
        """
        adapter = _RecordingAutobindAdapter()
        verifier = _FakeVerifier(verified=False)
        fix = HandlerPrLifecycleFix(
            occ_autobind_adapter=adapter,
            occ_companion_verifier=verifier,
        )
        orch = _make_orchestrator(fix)

        prs_dispatched = await _dispatch_one(orch, _occ_preflight_pr())

        assert prs_dispatched == 0, (
            "fail-closed: an unverified/unpushed OCC companion must NOT be "
            "counted in prs_fixed (this was the merge_sweep false-success)"
        )
        assert adapter.calls, "autobind adapter must still be invoked"
        assert verifier.calls, "the read-back verifier must run for the autobind arm"

    async def test_verified_companion_is_counted(self) -> None:
        """Happy path: a confirmed pushed companion counts exactly once."""
        adapter = _RecordingAutobindAdapter()
        fix = HandlerPrLifecycleFix(
            occ_autobind_adapter=adapter,
            occ_companion_verifier=_FakeVerifier(verified=True),
        )
        orch = _make_orchestrator(fix)

        prs_dispatched = await _dispatch_one(orch, _occ_preflight_pr())

        assert prs_dispatched == 1


@pytest.mark.unit
class TestFixHandlerVerificationField:
    """The fix handler surfaces occ_companion_verified from the verifier."""

    def _command(self) -> ModelPrLifecycleFixCommand:
        return ModelPrLifecycleFixCommand(
            correlation_id=uuid4(),
            pr_number=1651,
            repo="OmniNode-ai/omnimarket",
            block_reason=EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND,
            ticket_id="OMN-14173",
            requested_at=datetime.now(tz=UTC),
        )

    async def test_verified_true_sets_flag(self) -> None:
        fix = HandlerPrLifecycleFix(
            occ_autobind_adapter=_RecordingAutobindAdapter(),
            occ_companion_verifier=_FakeVerifier(verified=True),
        )
        result = await fix.handle(self._command())
        assert result.fix_applied is True
        assert result.occ_companion_verified is True

    async def test_unverified_sets_flag_false_and_notes_action(self) -> None:
        fix = HandlerPrLifecycleFix(
            occ_autobind_adapter=_RecordingAutobindAdapter(),
            occ_companion_verifier=_FakeVerifier(verified=False),
        )
        result = await fix.handle(self._command())
        # fix_applied stays True (the route ran) but the companion is unproven.
        assert result.fix_applied is True
        assert result.occ_companion_verified is False
        assert "NOT verified" in result.fix_action

    async def test_default_verifier_is_fail_closed(self) -> None:
        """No verifier wired → cannot prove the companion → not verified."""
        fix = HandlerPrLifecycleFix(occ_autobind_adapter=_RecordingAutobindAdapter())
        result = await fix.handle(self._command())
        assert result.occ_companion_verified is False
