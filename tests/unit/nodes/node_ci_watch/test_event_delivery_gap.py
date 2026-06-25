# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the CI event-delivery-gap detector + safe re-trigger (OMN-13416).

GitHub intermittently fails to deliver the workflow-dispatch event for some
required workflows on a fresh push. The affected required checks then produce
ZERO runs on HEAD while every other gate fires, leaving the PR stuck
``mergeStateStatus=BLOCKED`` with no failure to fix and no thing to re-run. The
proven manual recovery was an empty commit / re-dispatch to re-trigger event
delivery.

These tests pin:

* DETECTION — a required context that is absent from the PR's
  ``statusCheckRollup`` (never fired) on a BLOCKED PR is reported as an
  event-delivery gap, distinguished from a genuinely-pending check (present but
  not COMPLETED) and a genuinely-failing check (present, conclusion FAILURE).
* SAFE RE-TRIGGER — when a gap is detected and ``auto_retrigger`` is enabled,
  the handler performs ONE safe empty-commit re-trigger and reports
  terminal_status=RETRIGGERED, capped by ``max_retriggers`` so it can never
  loop.
* FAIL-SOFT — if the required-context query errors, NO gap is claimed (we never
  fabricate a gap from a failed query; that would re-trigger blindly).

All tests are offline: subprocess and the gh-query helpers are patched so no
real gh CLI runs and no commits are pushed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_ci_watch.handlers.handler_ci_watch import (
    EnumCiTerminalStatus,
    HandlerCiWatch,
    ModelCiStatusFetch,
    ModelCiWatchCommand,
    ModelEventDeliveryGap,
)


def _make_command(
    *,
    auto_fix: bool = False,
    auto_retrigger: bool = True,
    max_retriggers: int = 1,
    pr_number: int = 1325,
    repo: str = "OmniNode-ai/omnimarket",
    correlation_id: str = "test-corr-omn13416",
) -> ModelCiWatchCommand:
    return ModelCiWatchCommand(
        pr_number=pr_number,
        repo=repo,
        correlation_id=correlation_id,
        auto_fix=auto_fix,
        auto_retrigger=auto_retrigger,
        max_retriggers=max_retriggers,
        dry_run=False,
    )


# ---------------------------------------------------------------------------
# _detect_event_delivery_gap — pure classification
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDetectEventDeliveryGap:
    """A required context absent from the rollup on a BLOCKED PR is a gap."""

    def test_missing_required_context_is_a_gap(self) -> None:
        handler = HandlerCiWatch()
        gap = handler._detect_event_delivery_gap(
            required_contexts=["CI Summary", "Runtime Sweep", "node-drift-gate"],
            reported_contexts={"CI Summary", "node-drift-gate"},
            merge_state_status="BLOCKED",
            head_sha="abc123",
        )
        assert isinstance(gap, ModelEventDeliveryGap)
        assert gap.detected is True
        assert gap.missing_required_contexts == ["Runtime Sweep"]
        assert gap.head_sha == "abc123"

    def test_all_required_reported_is_not_a_gap(self) -> None:
        handler = HandlerCiWatch()
        gap = handler._detect_event_delivery_gap(
            required_contexts=["CI Summary", "Runtime Sweep"],
            reported_contexts={"CI Summary", "Runtime Sweep"},
            merge_state_status="BLOCKED",
            head_sha="abc123",
        )
        assert gap.detected is False
        assert gap.missing_required_contexts == []

    def test_no_gap_when_not_blocked(self) -> None:
        # A clean/behind PR with a not-yet-reported context is normal pending,
        # NOT an event-delivery gap. We only re-trigger BLOCKED PRs.
        handler = HandlerCiWatch()
        gap = handler._detect_event_delivery_gap(
            required_contexts=["CI Summary", "Runtime Sweep"],
            reported_contexts={"CI Summary"},
            merge_state_status="CLEAN",
            head_sha="abc123",
        )
        assert gap.detected is False

    def test_no_gap_when_no_required_contexts_known(self) -> None:
        # Fail-soft: if branch protection could not be read, never fabricate a
        # gap (empty required set) — that would re-trigger blindly.
        handler = HandlerCiWatch()
        gap = handler._detect_event_delivery_gap(
            required_contexts=[],
            reported_contexts={"CI Summary"},
            merge_state_status="BLOCKED",
            head_sha="abc123",
        )
        assert gap.detected is False


# ---------------------------------------------------------------------------
# handle(): green-looking-but-blocked + missing required context → RETRIGGERED
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandleRetrigger:
    """A detected gap on a no-failing-checks BLOCKED PR triggers ONE re-trigger."""

    def test_gap_detected_retriggers_once(self) -> None:
        handler = HandlerCiWatch()
        clean_fetch = ModelCiStatusFetch(
            failed_checks=[], failure_summary="", query_error=None
        )
        gap = ModelEventDeliveryGap(
            detected=True,
            missing_required_contexts=["Runtime Sweep"],
            head_sha="abc123",
            merge_state_status="BLOCKED",
            reason="1 required workflow never fired on HEAD",
        )
        with (
            patch.object(handler, "_fetch_ci_status", return_value=clean_fetch),
            patch.object(handler, "_probe_event_delivery_gap", return_value=gap),
            patch.object(handler, "_record_retrigger_attempt", return_value=1),
            patch.object(
                handler, "_retrigger_via_empty_commit", return_value=(True, "")
            ) as retrigger,
        ):
            result = handler.handle(_make_command(auto_retrigger=True))

        assert result.terminal_status == EnumCiTerminalStatus.RETRIGGERED
        assert "Runtime Sweep" in result.failure_summary
        retrigger.assert_called_once()

    def test_gap_not_retriggered_when_disabled(self) -> None:
        handler = HandlerCiWatch()
        clean_fetch = ModelCiStatusFetch(
            failed_checks=[], failure_summary="", query_error=None
        )
        gap = ModelEventDeliveryGap(
            detected=True,
            missing_required_contexts=["Runtime Sweep"],
            head_sha="abc123",
            merge_state_status="BLOCKED",
            reason="gap",
        )
        with (
            patch.object(handler, "_fetch_ci_status", return_value=clean_fetch),
            patch.object(handler, "_probe_event_delivery_gap", return_value=gap),
            patch.object(
                handler,
                "_retrigger_via_empty_commit",
                side_effect=AssertionError("must not re-trigger when disabled"),
            ),
        ):
            result = handler.handle(_make_command(auto_retrigger=False))

        # Disabled: report PASSED-looking checks; do not mutate the PR.
        assert result.terminal_status != EnumCiTerminalStatus.RETRIGGERED

    def test_no_gap_clean_checks_still_passes(self) -> None:
        handler = HandlerCiWatch()
        clean_fetch = ModelCiStatusFetch(
            failed_checks=[], failure_summary="", query_error=None
        )
        no_gap = ModelEventDeliveryGap(
            detected=False,
            missing_required_contexts=[],
            head_sha="abc123",
            merge_state_status="CLEAN",
            reason="",
        )
        with (
            patch.object(handler, "_fetch_ci_status", return_value=clean_fetch),
            patch.object(handler, "_probe_event_delivery_gap", return_value=no_gap),
        ):
            result = handler.handle(_make_command(auto_retrigger=True))
        assert result.terminal_status == EnumCiTerminalStatus.PASSED

    def test_failing_checks_skip_gap_probe(self) -> None:
        # A real failure is handled by the existing failed/auto-fix path; the
        # gap probe is only relevant when there are no failing checks.
        handler = HandlerCiWatch()
        failing = ModelCiStatusFetch(
            failed_checks=[
                __import__(
                    "omnimarket.nodes.node_ci_watch.handlers.handler_ci_watch",
                    fromlist=["ModelFailedCheck"],
                ).ModelFailedCheck(name="lint", conclusion="failure")
            ],
            failure_summary="lint failed",
            query_error=None,
        )
        with (
            patch.object(handler, "_fetch_ci_status", return_value=failing),
            patch.object(
                handler,
                "_probe_event_delivery_gap",
                side_effect=AssertionError("must not probe gap on real failures"),
            ),
        ):
            result = handler.handle(_make_command(auto_fix=False))
        assert result.terminal_status == EnumCiTerminalStatus.FAILED

    def test_retrigger_cap_zero_does_not_retrigger(self) -> None:
        handler = HandlerCiWatch()
        clean_fetch = ModelCiStatusFetch(
            failed_checks=[], failure_summary="", query_error=None
        )
        gap = ModelEventDeliveryGap(
            detected=True,
            missing_required_contexts=["Runtime Sweep"],
            head_sha="abc123",
            merge_state_status="BLOCKED",
            reason="gap",
        )
        with (
            patch.object(handler, "_fetch_ci_status", return_value=clean_fetch),
            patch.object(handler, "_probe_event_delivery_gap", return_value=gap),
            patch.object(
                handler,
                "_retrigger_via_empty_commit",
                side_effect=AssertionError("max_retriggers=0 must not re-trigger"),
            ),
        ):
            result = handler.handle(
                _make_command(auto_retrigger=True, max_retriggers=0)
            )
        assert result.terminal_status != EnumCiTerminalStatus.RETRIGGERED

    def test_retrigger_cap_exhaustion_does_not_push_again(self) -> None:
        handler = HandlerCiWatch()
        clean_fetch = ModelCiStatusFetch(
            failed_checks=[], failure_summary="", query_error=None
        )
        gap = ModelEventDeliveryGap(
            detected=True,
            missing_required_contexts=["Runtime Sweep"],
            head_sha="abc123",
            merge_state_status="BLOCKED",
            reason="gap",
        )
        with (
            patch.object(handler, "_fetch_ci_status", return_value=clean_fetch),
            patch.object(handler, "_probe_event_delivery_gap", return_value=gap),
            patch.object(handler, "_record_retrigger_attempt", return_value=2),
            patch.object(
                handler,
                "_retrigger_via_empty_commit",
                side_effect=AssertionError("must not push after cap exhaustion"),
            ),
        ):
            result = handler.handle(
                _make_command(auto_retrigger=True, max_retriggers=1)
            )

        assert result.terminal_status == EnumCiTerminalStatus.ERROR
        assert "max_retriggers=1 is exhausted" in result.failure_summary


# ---------------------------------------------------------------------------
# _probe_event_delivery_gap — fail-soft on query error
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProbeFailSoft:
    """A required-context query error yields no gap (never fabricate a gap)."""

    def test_required_contexts_query_error_no_gap(self) -> None:
        handler = HandlerCiWatch()
        # branch-protection fetch errors → empty required set → no gap
        completed = MagicMock()
        completed.returncode = 1
        completed.stdout = ""
        completed.stderr = "Not Found"
        with patch(
            "omnimarket.nodes.node_ci_watch.handlers.handler_ci_watch.subprocess.run",
            return_value=completed,
        ):
            gap = handler._probe_event_delivery_gap(
                repo="OmniNode-ai/omnimarket",
                pr_number=1325,
                base_branch="dev",
            )
        assert gap.detected is False
