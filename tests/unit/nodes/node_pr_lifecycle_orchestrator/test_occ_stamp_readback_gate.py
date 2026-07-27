# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14191 — merge-sweep gates prs_fixed on a read-back, never on dispatch.

Piece 5/5 of the OMN-14180 canonical OCC stamp-model. Generalizes the OMN-14173
autobind read-back to EVERY fix arm and closes OMN-14174's dispatch-vs-effect
over-count: the orchestrator counts ``prs_fixed`` for an arm ONLY after an
independent read-back re-reads the ACTUAL pushed OCC companion PR + the product
PR body (parsed via the Piece-2 canonical parser) and confirms the stamp landed.

The read-back logic runs for real here (real ``parse_pr_occ_metadata_stamp``,
real :class:`OccStampReadback`); only the gh/remote fetch is faked with an
in-memory GitHub state, so a "noop adapter that dispatches but changes nothing"
is modelled faithfully (it simply never mutates the fake remote) rather than
stubbed with a boolean. No gh/network.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)

from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    ModelPrLifecycleFixCommand,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_result import (
    ModelPrLifecycleFixResult,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    HandlerPrLifecycleOrchestrator,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.occ_stamp_readback import (
    OccStampReadback,
    _UnverifiedOccStampReadback,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumPrCategory,
    TriageRecord,
)

_PRODUCT_REPO = "OmniNode-ai/omnimarket"
_OCC_REPO = "OmniNode-ai/onex_change_control"
_OCC_PREFLIGHT = "occ-preflight / eligibility"
_TICKET = "OMN-14173"


# ---------------------------------------------------------------------------
# In-memory GitHub remote — models exactly what a live `gh pr view` returns.
# ---------------------------------------------------------------------------


class _FakeRemote:
    """A tiny in-memory GitHub, keyed by (repo, pr_number)."""

    def __init__(self) -> None:
        self._prs: dict[tuple[str, int], dict[str, object]] = {}

    def set_pr(self, repo: str, pr_number: int, *, body: str, state: str) -> None:
        self._prs[(repo, pr_number)] = {
            "number": pr_number,
            "body": body,
            "state": state,
        }

    def fetch_pr(self, repo: str, pr_number: int) -> dict[str, object]:
        try:
            return dict(self._prs[(repo, pr_number)])
        except KeyError as exc:
            # Mirrors a `gh pr view` non-zero exit for a missing PR.
            raise RuntimeError(f"no such PR {repo}#{pr_number}") from exc


def _landed_stamp_body(occ_pr_number: int, ticket: str = _TICKET) -> str:
    """A product PR body carrying the canonical landed OCC stamp."""
    return (
        "Fixes the thing.\n\n"
        "## Evidence\n\n"
        f"Evidence-Ticket: {ticket}\n"
        f"Evidence-Source: OCC#{occ_pr_number}\n"
    )


# ---------------------------------------------------------------------------
# Fake fix handler — dispatches, optionally mutating the fake remote to model
# whether the arm actually LANDED an OCC companion.
# ---------------------------------------------------------------------------


class _FakeFixHandler:
    """Returns fix_applied=True (a dispatch); an on_dispatch hook lands effects."""

    def __init__(
        self,
        remote: _FakeRemote,
        *,
        on_dispatch: Callable[[_FakeRemote, ModelPrLifecycleFixCommand], None]
        | None = None,
    ) -> None:
        self._remote = remote
        self._on_dispatch = on_dispatch
        self.calls: list[ModelPrLifecycleFixCommand] = []

    async def handle(
        self, command: ModelPrLifecycleFixCommand
    ) -> ModelPrLifecycleFixResult:
        self.calls.append(command)
        if self._on_dispatch is not None:
            self._on_dispatch(self._remote, command)
        return ModelPrLifecycleFixResult(
            correlation_id=command.correlation_id,
            pr_number=command.pr_number,
            repo=command.repo,
            block_reason=command.block_reason,
            fix_applied=True,
            fix_action="[fake] dispatched",
            completed_at=datetime.now(tz=UTC),
        )


def _occ_preflight_pr(pr_number: int = 1651) -> TriageRecord:
    """A green-except-OCC-companion PR: only occ-preflight fails, ticket present.

    Routes to the RECEIPT_EVIDENCE_SOURCE_AUTOBIND arm (OMN-14173 shape).
    """
    return TriageRecord(
        pr_number=pr_number,
        repo=_PRODUCT_REPO,
        category=EnumPrCategory.RED,
        ticket_ids=(_TICKET,),
        failed_check_names=(_OCC_PREFLIGHT,),
    )


def _mixed_failure_pr(pr_number: int = 1652) -> TriageRecord:
    """A mixed-failure PR (typecheck + occ-preflight red): routes to CODE_FAILURE.

    This is the exact OMN-14174 over-count shape — a non-OCC arm that used to
    count on dispatch even though nothing OCC landed.
    """
    return TriageRecord(
        pr_number=pr_number,
        repo=_PRODUCT_REPO,
        category=EnumPrCategory.RED,
        ticket_ids=(_TICKET,),
        failed_check_names=("typecheck", _OCC_PREFLIGHT),
    )


def _make_orchestrator(
    remote: _FakeRemote, fix_handler: _FakeFixHandler
) -> HandlerPrLifecycleOrchestrator:
    return HandlerPrLifecycleOrchestrator(
        event_bus=MagicMock(spec=ProtocolEventBusPublisher),
        fix=fix_handler,
        occ_stamp_readback=OccStampReadback(fetcher=remote, occ_repo=_OCC_REPO),
    )


async def _prs_fixed(
    orch: HandlerPrLifecycleOrchestrator, pr: TriageRecord, *, dry_run: bool = False
) -> int:
    results = await orch._dispatch_fix_parallel(
        fix_prs=(pr,),
        correlation_id=uuid4(),
        dry_run=dry_run,
        max_parallel=1,
        enable_admin_merge_fallback=False,
        admin_fallback_threshold_minutes=30,
    )
    return sum(r.prs_dispatched for r in results)


# ---------------------------------------------------------------------------
# The three required regression tests.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReadBackGate:
    async def test_false_success_noop_dispatch_counts_zero(self) -> None:
        """OMN-14173 shape: the arm dispatches cleanly but lands NO companion.

        The product PR body is never stamped and no OCC PR is created, so the
        read-back parses an empty stamp and fails closed -> prs_fixed == 0.
        """
        remote = _FakeRemote()
        # Product PR exists but its body is unchanged (no Evidence stamp).
        remote.set_pr(_PRODUCT_REPO, 1651, body="Fixes the thing.\n", state="OPEN")
        # on_dispatch = None -> the fake fixer changes nothing (the noop adapter).
        fix = _FakeFixHandler(remote, on_dispatch=None)
        orch = _make_orchestrator(remote, fix)

        prs_fixed = await _prs_fixed(orch, _occ_preflight_pr(1651))

        assert prs_fixed == 0, (
            "fail-closed: a dispatch that lands no OCC companion must NOT count "
            "in prs_fixed (the merge_sweep --fix-only false-success)"
        )
        assert fix.calls, "the fix arm must still be dispatched"

    async def test_true_success_landed_companion_counts_one(self) -> None:
        """A real landed effect: the arm stamps the body AND opens the OCC PR."""
        remote = _FakeRemote()
        remote.set_pr(_PRODUCT_REPO, 1651, body="Fixes the thing.\n", state="OPEN")

        def _land(r: _FakeRemote, cmd: ModelPrLifecycleFixCommand) -> None:
            # The fixer pushed a companion: stamp the product body + open OCC#7788.
            r.set_pr(_OCC_REPO, 7788, body="OCC receipt", state="OPEN")
            r.set_pr(
                cmd.repo, cmd.pr_number, body=_landed_stamp_body(7788), state="OPEN"
            )

        fix = _FakeFixHandler(remote, on_dispatch=_land)
        orch = _make_orchestrator(remote, fix)

        prs_fixed = await _prs_fixed(orch, _occ_preflight_pr(1651))

        assert prs_fixed == 1, "a confirmed pushed OCC companion counts exactly once"

    async def test_over_count_dispatched_not_landed_counts_zero(self) -> None:
        """OMN-14174 shape: a mixed-failure PR routes to CODE_FAILURE and

        dispatches, but lands no OCC stamp. It used to count on dispatch; now the
        read-back finds nothing -> prs_fixed == 0.
        """
        remote = _FakeRemote()
        remote.set_pr(_PRODUCT_REPO, 1652, body="Fixes the thing.\n", state="OPEN")
        fix = _FakeFixHandler(remote, on_dispatch=None)
        orch = _make_orchestrator(remote, fix)

        prs_fixed = await _prs_fixed(orch, _mixed_failure_pr(1652))

        assert prs_fixed == 0, (
            "fail-closed: a dispatched-but-not-landed non-OCC arm must NOT count "
            "in prs_fixed (OMN-14174 dispatch-vs-effect over-count)"
        )
        assert fix.calls, "the fix arm must still be dispatched"

    async def test_stamp_present_but_occ_pr_missing_counts_zero(self) -> None:
        """Half-landed: the product body is stamped but the OCC PR does not exist.

        Exercises the (a) OCC-companion-exists half of the gate independently.
        """
        remote = _FakeRemote()

        def _stamp_only(r: _FakeRemote, cmd: ModelPrLifecycleFixCommand) -> None:
            # Body claims OCC#9999 but no such OCC PR was ever pushed.
            r.set_pr(
                cmd.repo, cmd.pr_number, body=_landed_stamp_body(9999), state="OPEN"
            )

        remote.set_pr(_PRODUCT_REPO, 1651, body="Fixes the thing.\n", state="OPEN")
        fix = _FakeFixHandler(remote, on_dispatch=_stamp_only)
        orch = _make_orchestrator(remote, fix)

        prs_fixed = await _prs_fixed(orch, _occ_preflight_pr(1651))

        assert prs_fixed == 0, (
            "fail-closed: an Evidence-Source pointing at a non-existent OCC PR "
            "must NOT count"
        )

    async def test_dry_run_never_counts_and_never_reads_back(self) -> None:
        """dry_run lands nothing, so it is never counted (and never fetches)."""
        remote = _FakeRemote()
        # Even if a stamp somehow existed, dry_run must short-circuit to 0.
        remote.set_pr(_OCC_REPO, 7788, body="OCC receipt", state="OPEN")
        remote.set_pr(_PRODUCT_REPO, 1651, body=_landed_stamp_body(7788), state="OPEN")
        fix = _FakeFixHandler(remote, on_dispatch=None)
        orch = _make_orchestrator(remote, fix)

        prs_fixed = await _prs_fixed(orch, _occ_preflight_pr(1651), dry_run=True)

        assert prs_fixed == 0


# ---------------------------------------------------------------------------
# Unit coverage for the read-back logic itself (real Piece-2 parser).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOccStampReadbackLogic:
    async def test_valid_stamp_and_open_occ_pr_verifies(self) -> None:
        remote = _FakeRemote()
        remote.set_pr(_OCC_REPO, 42, body="receipt", state="OPEN")
        remote.set_pr(_PRODUCT_REPO, 100, body=_landed_stamp_body(42), state="OPEN")
        rb = OccStampReadback(fetcher=remote, occ_repo=_OCC_REPO)

        result = await rb.verify_fix_landed(_PRODUCT_REPO, 100, _TICKET)

        assert result.verified is True
        assert result.occ_pr_number == 42
        assert result.evidence_source_present is True
        assert result.occ_pr_open is True
        assert _TICKET in result.evidence_tickets

    async def test_no_stamp_is_not_verified(self) -> None:
        remote = _FakeRemote()
        remote.set_pr(_PRODUCT_REPO, 100, body="no evidence here", state="OPEN")
        rb = OccStampReadback(fetcher=remote, occ_repo=_OCC_REPO)

        result = await rb.verify_fix_landed(_PRODUCT_REPO, 100, _TICKET)

        assert result.verified is False
        assert result.evidence_source_present is False
        assert "no OCC companion landed" in result.reason

    async def test_closed_occ_pr_is_not_verified(self) -> None:
        remote = _FakeRemote()
        remote.set_pr(_OCC_REPO, 42, body="receipt", state="CLOSED")
        remote.set_pr(_PRODUCT_REPO, 100, body=_landed_stamp_body(42), state="OPEN")
        rb = OccStampReadback(fetcher=remote, occ_repo=_OCC_REPO)

        result = await rb.verify_fix_landed(_PRODUCT_REPO, 100, _TICKET)

        assert result.verified is False
        assert result.occ_pr_open is False
        assert "not open" in result.reason

    async def test_ticket_mismatch_is_not_verified(self) -> None:
        remote = _FakeRemote()
        remote.set_pr(_OCC_REPO, 42, body="receipt", state="OPEN")
        remote.set_pr(_PRODUCT_REPO, 100, body=_landed_stamp_body(42), state="OPEN")
        rb = OccStampReadback(fetcher=remote, occ_repo=_OCC_REPO)

        result = await rb.verify_fix_landed(_PRODUCT_REPO, 100, "OMN-99999")

        assert result.verified is False
        assert "does not include" in result.reason

    async def test_product_pr_unreadable_is_not_verified(self) -> None:
        remote = _FakeRemote()  # product PR absent -> fetch raises
        rb = OccStampReadback(fetcher=remote, occ_repo=_OCC_REPO)

        result = await rb.verify_fix_landed(_PRODUCT_REPO, 100, _TICKET)

        assert result.verified is False
        assert "could not read product PR" in result.reason

    async def test_unverified_default_readback_fails_closed(self) -> None:
        rb = _UnverifiedOccStampReadback()

        result = await rb.verify_fix_landed(_PRODUCT_REPO, 100, _TICKET)

        assert result.verified is False
        assert "fail-closed" in result.reason
