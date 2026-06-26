# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for golden chain sweep per-chain freshness/recency assertion (OMN-13639).

Today the sweep asserts field-presence on the latest tail row but not recency,
so a chain reads green when its only matching row is a weeks-old fixture/seed
and no fresh flow has occurred since. These tests cover the new per-chain
``max_row_age_seconds`` threshold: when the latest tail row's timestamp exceeds
the threshold, the chain is downgraded to a distinct ``STALE``/WARN tri-state
(not PASS) and the row age is reported.

The handler stays a pure, deterministic compute node — the reference "now" is
injected via the request (``now_iso``) rather than read from the system clock.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
    EnumChainStatus,
    EnumSweepStatus,
    GoldenChainSweepRequest,
    ModelChainDefinition,
    NodeGoldenChainSweep,
)

# Reference clock injected into every freshness test so the compute stays
# deterministic (no system-clock read inside the handler).
_NOW_ISO = "2026-06-26T12:00:00+00:00"

# A fresh row: 5 minutes old relative to _NOW_ISO.
_FRESH_TS = "2026-06-26T11:55:00+00:00"
# A stale row: ~4 days old relative to _NOW_ISO (mirrors pattern_learning fixture).
_STALE_TS = "2026-06-22T16:34:00+00:00"

# A chain that requires the projected row to be at most 1 hour old.
_FRESH_CHAIN = ModelChainDefinition(
    name="pattern_learning",
    head_topic="onex.evt.omniintelligence.pattern-stored.v1",
    tail_table="pattern_learning_artifacts",
    expected_fields=["correlation_id"],
    timestamp_field="created_at",
    max_row_age_seconds=3600,
)


@pytest.mark.unit
class TestPerChainFreshness:
    """``max_row_age_seconds`` downgrades stale-but-field-complete chains to STALE."""

    def test_fresh_row_passes(self) -> None:
        """A row inside the freshness window with all fields present is PASS."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_FRESH_CHAIN],
            projected_rows={
                "pattern_learning": {
                    "correlation_id": "abc",
                    "created_at": _FRESH_TS,
                }
            },
            now_iso=_NOW_ISO,
        )
        result = handler.handle(request)

        assert result.chain_results[0].status == EnumChainStatus.PASS
        assert result.overall_status == EnumSweepStatus.PASS
        assert result.chains_passed == 1
        assert result.chains_stale == 0

    def test_stale_row_downgrades_to_stale_not_pass(self) -> None:
        """A row OLDER than the threshold (but field-complete) is STALE, not PASS."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_FRESH_CHAIN],
            projected_rows={
                "pattern_learning": {
                    "correlation_id": "22222222-2222-2222-2222-222222222222",
                    "created_at": _STALE_TS,
                }
            },
            now_iso=_NOW_ISO,
        )
        result = handler.handle(request)

        cr = result.chain_results[0]
        assert cr.status == EnumChainStatus.STALE
        assert cr.status != EnumChainStatus.PASS
        # Distinct overall tri-state — green requires recent flow.
        assert result.overall_status == EnumSweepStatus.WARN
        assert result.chains_stale == 1
        assert result.chains_passed == 0
        assert result.chains_failed == 0

    def test_stale_message_reports_row_age(self) -> None:
        """The STALE chain reports the row age and the configured threshold."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_FRESH_CHAIN],
            projected_rows={
                "pattern_learning": {
                    "correlation_id": "abc",
                    "created_at": _STALE_TS,
                }
            },
            now_iso=_NOW_ISO,
        )
        result = handler.handle(request)

        cr = result.chain_results[0]
        # 2026-06-22T16:34 -> 2026-06-26T12:00 == 3 days, 19h26m == 329160s.
        assert cr.row_age_seconds == 329160.0
        assert "stale" in cr.message.lower()
        # row age + threshold both surfaced for the operator.
        assert "329160" in cr.message
        assert "3600" in cr.message

    def test_no_threshold_skips_freshness_check(self) -> None:
        """A chain WITHOUT ``max_row_age_seconds`` is never downgraded for age.

        Default behaviour is unchanged — old rows still PASS when no freshness
        threshold is configured (back-compat with all existing chains).
        """
        handler = NodeGoldenChainSweep()
        chain_no_threshold = ModelChainDefinition(
            name="evaluation",
            head_topic="onex.evt.omniclaude.session-outcome.v1",
            tail_table="session_outcomes",
            expected_fields=["session_id"],
        )
        request = GoldenChainSweepRequest(
            chains=[chain_no_threshold],
            projected_rows={
                "evaluation": {
                    "session_id": "sess-001",
                    "created_at": "2026-05-29T19:57:00+00:00",  # ~4 weeks old
                }
            },
            now_iso=_NOW_ISO,
        )
        result = handler.handle(request)

        assert result.chain_results[0].status == EnumChainStatus.PASS
        assert result.chains_stale == 0

    def test_missing_fields_takes_precedence_over_freshness(self) -> None:
        """Field-presence FAIL is reported before freshness — FAIL beats STALE."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_FRESH_CHAIN],
            projected_rows={
                "pattern_learning": {
                    # missing correlation_id; stale timestamp present.
                    "created_at": _STALE_TS,
                }
            },
            now_iso=_NOW_ISO,
        )
        result = handler.handle(request)

        cr = result.chain_results[0]
        assert cr.status == EnumChainStatus.FAIL
        assert "correlation_id" in cr.missing_fields
        assert result.overall_status == EnumSweepStatus.FAIL

    def test_missing_timestamp_field_is_stale_not_pass(self) -> None:
        """A freshness-gated chain whose row lacks the timestamp column is STALE.

        Recency cannot be proven, so the chain must not read green — it is a
        warning, not a hard fail (the projection could simply lack the column).
        """
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_FRESH_CHAIN],
            projected_rows={
                "pattern_learning": {
                    "correlation_id": "abc",
                    # no created_at present
                }
            },
            now_iso=_NOW_ISO,
        )
        result = handler.handle(request)

        cr = result.chain_results[0]
        assert cr.status == EnumChainStatus.STALE
        assert result.overall_status == EnumSweepStatus.WARN
        assert "created_at" in cr.message

    def test_unparseable_timestamp_is_stale_not_pass(self) -> None:
        """A non-ISO timestamp value cannot prove recency → STALE, not PASS."""
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_FRESH_CHAIN],
            projected_rows={
                "pattern_learning": {
                    "correlation_id": "abc",
                    "created_at": "not-a-timestamp",
                }
            },
            now_iso=_NOW_ISO,
        )
        result = handler.handle(request)

        assert result.chain_results[0].status == EnumChainStatus.STALE
        assert result.overall_status == EnumSweepStatus.WARN

    def test_missing_now_iso_with_freshness_chain_fails_fast(self) -> None:
        """A freshness-gated chain without an injected ``now_iso`` fails fast.

        The compute is pure — it must not read the system clock. A caller that
        configures a freshness threshold but supplies no reference clock is a
        wiring bug, surfaced as ERROR (not a silent PASS).
        """
        handler = NodeGoldenChainSweep()
        request = GoldenChainSweepRequest(
            chains=[_FRESH_CHAIN],
            projected_rows={
                "pattern_learning": {
                    "correlation_id": "abc",
                    "created_at": _FRESH_TS,
                }
            },
            # now_iso intentionally omitted
        )
        result = handler.handle(request)

        cr = result.chain_results[0]
        assert cr.status == EnumChainStatus.ERROR
        assert result.overall_status == EnumSweepStatus.FAIL

    def test_mixed_pass_and_stale_overall_warn(self) -> None:
        """A PASS chain + a STALE chain → overall WARN (non-blocking warning)."""
        handler = NodeGoldenChainSweep()
        passing = ModelChainDefinition(
            name="delegation",
            head_topic="onex.evt.omniclaude.task-delegated.v1",
            tail_table="delegation_events",
            expected_fields=["correlation_id"],
            timestamp_field="timestamp",
            max_row_age_seconds=3600,
        )
        request = GoldenChainSweepRequest(
            chains=[passing, _FRESH_CHAIN],
            projected_rows={
                "delegation": {"correlation_id": "fresh", "timestamp": _FRESH_TS},
                "pattern_learning": {"correlation_id": "old", "created_at": _STALE_TS},
            },
            now_iso=_NOW_ISO,
        )
        result = handler.handle(request)

        statuses = {r.name: r.status for r in result.chain_results}
        assert statuses["delegation"] == EnumChainStatus.PASS
        assert statuses["pattern_learning"] == EnumChainStatus.STALE
        assert result.chains_passed == 1
        assert result.chains_stale == 1
        assert result.overall_status == EnumSweepStatus.WARN

    def test_stale_plus_failed_is_partial(self) -> None:
        """STALE (warning) + FAIL (blocking) → PARTIAL, not WARN."""
        handler = NodeGoldenChainSweep()
        failing = ModelChainDefinition(
            name="delegation",
            head_topic="onex.evt.omniclaude.task-delegated.v1",
            tail_table="delegation_events",
            expected_fields=["correlation_id", "must_have"],
        )
        request = GoldenChainSweepRequest(
            chains=[failing, _FRESH_CHAIN],
            projected_rows={
                "delegation": {"correlation_id": "x"},  # missing must_have
                "pattern_learning": {"correlation_id": "old", "created_at": _STALE_TS},
            },
            now_iso=_NOW_ISO,
        )
        result = handler.handle(request)

        assert result.chains_failed == 1
        assert result.chains_stale == 1
        assert result.overall_status == EnumSweepStatus.PARTIAL

    def test_boundary_exactly_at_threshold_passes(self) -> None:
        """A row exactly at the age threshold is still fresh (PASS)."""
        handler = NodeGoldenChainSweep()
        # exactly 3600s before _NOW_ISO
        request = GoldenChainSweepRequest(
            chains=[_FRESH_CHAIN],
            projected_rows={
                "pattern_learning": {
                    "correlation_id": "abc",
                    "created_at": "2026-06-26T11:00:00+00:00",
                }
            },
            now_iso=_NOW_ISO,
        )
        result = handler.handle(request)

        assert result.chain_results[0].status == EnumChainStatus.PASS
        assert result.chain_results[0].row_age_seconds == 3600.0


@pytest.mark.unit
class TestFreshnessRegistryWiring:
    """The packaged registry exposes the freshness threshold on stale chains."""

    def test_registry_pattern_learning_and_evaluation_have_thresholds(self) -> None:
        from omnimarket.nodes.node_golden_chain_sweep.registry import load_registry

        chain_map = {c.name: c for c in load_registry()}

        pl = chain_map["pattern_learning"]
        assert pl.max_row_age_seconds is not None
        assert pl.max_row_age_seconds > 0
        assert pl.timestamp_field

        ev = chain_map["evaluation"]
        assert ev.max_row_age_seconds is not None
        assert ev.max_row_age_seconds > 0
        assert ev.timestamp_field
