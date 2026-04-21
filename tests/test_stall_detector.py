# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the PR snapshot stall detector.

Covers the four invariants from OMN-9404 DoD:
  1. Identical snapshots emit a stall event.
  2. head_sha change does NOT emit a stall event.
  3. Labels-only change does NOT emit a stall event (labels not in shape).
  4. First-run (no previous snapshot) emits no stall events.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_merge_sweep.handlers.handler_merge_sweep import ModelPRInfo
from omnimarket.nodes.node_pr_snapshot_effect.handlers.handler_pr_snapshot import (
    HandlerPrSnapshot,
    _detect_stalls,
    _load_previous_snapshot,
    _write_snapshot,
)
from omnimarket.nodes.node_pr_snapshot_effect.models.model_pr_snapshot_input import (
    ModelPrSnapshotInput,
)
from omnimarket.nodes.node_pr_snapshot_effect.models.model_pr_stall_event import (
    ModelPrStallEvent,
)

_NOW = datetime(2026, 4, 21, 12, 0, 0, tzinfo=UTC)


def _make_pr(
    number: int = 1,
    repo: str = "OmniNode-ai/omniclaude",
    mergeable: str = "CONFLICTING",
    merge_state_status: str = "BLOCKED",
    review_decision: str | None = None,
    required_checks_pass: bool = False,
    head_sha: str | None = "abc123",
    labels: list[str] | None = None,
) -> ModelPRInfo:
    return ModelPRInfo(
        number=number,
        title="test PR",
        repo=repo,
        mergeable=mergeable,
        merge_state_status=merge_state_status,
        review_decision=review_decision,
        required_checks_pass=required_checks_pass,
        head_sha=head_sha,
        labels=labels or [],
    )


def _pr_to_dict(pr: ModelPRInfo) -> dict[str, Any]:
    return pr.model_dump()


@pytest.mark.unit
class TestDetectStallsInvariant1:
    """Identical blocked PRs across two snapshots must emit a stall event."""

    def test_identical_snapshots_emit_stall(self) -> None:
        pr = _make_pr(number=10, mergeable="CONFLICTING", merge_state_status="BLOCKED")
        previous_raw = [_pr_to_dict(pr)]

        stalls = _detect_stalls([pr], previous_raw, _NOW)

        assert len(stalls) == 1
        assert stalls[0].pr_number == 10
        assert stalls[0].repo == "OmniNode-ai/omniclaude"
        assert stalls[0].stall_count == 2

    def test_stall_event_includes_blocking_reason(self) -> None:
        pr = _make_pr(
            mergeable="CONFLICTING",
            merge_state_status="BLOCKED",
            required_checks_pass=False,
        )
        stalls = _detect_stalls([pr], [_pr_to_dict(pr)], _NOW)

        assert len(stalls) == 1
        assert "mergeable=CONFLICTING" in stalls[0].blocking_reason
        assert "merge_state_status=BLOCKED" in stalls[0].blocking_reason

    def test_multiple_stalled_prs_all_emitted(self) -> None:
        prs = [
            _make_pr(number=1, mergeable="CONFLICTING", merge_state_status="BLOCKED"),
            _make_pr(number=2, mergeable="UNKNOWN", merge_state_status="UNKNOWN"),
        ]
        previous_raw = [_pr_to_dict(p) for p in prs]

        stalls = _detect_stalls(prs, previous_raw, _NOW)

        assert len(stalls) == 2
        assert {s.pr_number for s in stalls} == {1, 2}


@pytest.mark.unit
class TestDetectStallsInvariant2:
    """head_sha change must suppress stall event even if all other fields match."""

    def test_head_sha_change_suppresses_stall(self) -> None:
        pr_prev = _make_pr(number=5, head_sha="old_sha")
        pr_curr = _make_pr(number=5, head_sha="new_sha")

        stalls = _detect_stalls([pr_curr], [_pr_to_dict(pr_prev)], _NOW)

        assert len(stalls) == 0

    def test_same_head_sha_still_stalls(self) -> None:
        pr = _make_pr(number=5, head_sha="same_sha")

        stalls = _detect_stalls([pr], [_pr_to_dict(pr)], _NOW)

        assert len(stalls) == 1


@pytest.mark.unit
class TestDetectStallsInvariant3:
    """Labels-only change must NOT suppress stall (labels excluded from shape)."""

    def test_label_change_does_not_suppress_stall(self) -> None:
        pr_prev = _make_pr(number=7, labels=["urgent"])
        pr_curr = _make_pr(number=7, labels=["urgent", "auto-stall-detected"])

        stalls = _detect_stalls([pr_curr], [_pr_to_dict(pr_prev)], _NOW)

        assert len(stalls) == 1
        assert stalls[0].pr_number == 7


@pytest.mark.unit
class TestDetectStallsInvariant4:
    """First-run with no previous snapshot must emit no stall events."""

    def test_no_previous_snapshot_no_stalls(self) -> None:
        pr = _make_pr(number=3, mergeable="CONFLICTING", merge_state_status="BLOCKED")

        stalls = _detect_stalls([pr], None, _NOW)

        assert len(stalls) == 0

    def test_pr_not_in_previous_no_stall(self) -> None:
        pr_new = _make_pr(number=99)
        pr_existing = _make_pr(number=1)

        stalls = _detect_stalls([pr_new], [_pr_to_dict(pr_existing)], _NOW)

        assert len(stalls) == 0


@pytest.mark.unit
class TestDetectStallsCleanPrs:
    """Clean (MERGEABLE + CLEAN) PRs must never emit stall events."""

    def test_clean_mergeable_pr_does_not_stall(self) -> None:
        pr = _make_pr(
            number=11,
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
            required_checks_pass=True,
        )
        stalls = _detect_stalls([pr], [_pr_to_dict(pr)], _NOW)

        assert len(stalls) == 0

    def test_only_blocked_prs_stall_when_mixed(self) -> None:
        clean_pr = _make_pr(
            number=1,
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
            required_checks_pass=True,
        )
        blocked_pr = _make_pr(
            number=2, mergeable="CONFLICTING", merge_state_status="BLOCKED"
        )
        prs = [clean_pr, blocked_pr]
        previous_raw = [_pr_to_dict(p) for p in prs]

        stalls = _detect_stalls(prs, previous_raw, _NOW)

        assert len(stalls) == 1
        assert stalls[0].pr_number == 2


@pytest.mark.unit
class TestSnapshotDiskPersistence:
    """write_snapshot / load_previous_snapshot round-trip."""

    def test_write_then_load_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_dir = Path(tmpdir)
            pr = _make_pr(number=42)

            # Before first write: no current.json → load returns None
            assert _load_previous_snapshot(snapshot_dir) is None

            _write_snapshot([pr], snapshot_dir)
            # After first write: current.json exists
            assert (snapshot_dir / "current.json").exists()
            assert not (snapshot_dir / "previous.json").exists()

            # Load before second write reads from current.json
            loaded = _load_previous_snapshot(snapshot_dir)
            assert loaded is not None
            assert len(loaded) == 1
            assert loaded[0]["number"] == 42

            # Second write rotates current → previous, writes new current
            _write_snapshot([pr], snapshot_dir)
            assert (snapshot_dir / "previous.json").exists()

    def test_load_returns_none_when_no_previous(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _load_previous_snapshot(Path(tmpdir))
            assert result is None


@pytest.mark.unit
class TestHandlerIntegrationWithStalls:
    """End-to-end handler invocation producing stall_events on ModelPrSnapshotResult."""

    def _make_gh_pr_json(
        self,
        number: int = 1,
        mergeable: str = "CONFLICTING",
        merge_state_status: str = "BLOCKED",
        head_ref_oid: str = "abc123",
    ) -> dict[str, object]:
        return {
            "number": number,
            "title": "test",
            "mergeable": mergeable,
            "mergeStateStatus": merge_state_status,
            "isDraft": False,
            "reviewDecision": None,
            "statusCheckRollup": [{"conclusion": "FAILURE"}],
            "labels": [],
            "headRefOid": head_ref_oid,
        }

    def _mock_run(self, data: list[dict[str, object]]) -> MagicMock:
        m = MagicMock()
        m.returncode = 0
        m.stdout = json.dumps(data)
        m.stderr = ""
        return m

    def test_stall_events_emitted_on_second_identical_scan(self) -> None:
        pr_data = [self._make_gh_pr_json(number=10)]
        input_model = ModelPrSnapshotInput(repos=("OmniNode-ai/omniclaude",))

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("subprocess.run", return_value=self._mock_run(pr_data)),
            patch(
                "omnimarket.nodes.node_pr_snapshot_effect.handlers.handler_pr_snapshot._snapshot_dir",
                return_value=Path(tmpdir),
            ),
        ):
            handler = HandlerPrSnapshot()
            # First scan: no previous snapshot → no stalls
            result1 = handler.handle(input_model)
            assert len(result1.stall_events) == 0

            # Second scan: identical shape → stall detected
            result2 = handler.handle(input_model)
            assert len(result2.stall_events) == 1
            assert result2.stall_events[0].pr_number == 10
            assert isinstance(result2.stall_events[0], ModelPrStallEvent)

    def test_no_stall_when_head_sha_changes_between_scans(self) -> None:
        input_model = ModelPrSnapshotInput(repos=("OmniNode-ai/omniclaude",))

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch(
                "omnimarket.nodes.node_pr_snapshot_effect.handlers.handler_pr_snapshot._snapshot_dir",
                return_value=Path(tmpdir),
            ),
        ):
            handler = HandlerPrSnapshot()

            with patch(
                "subprocess.run",
                return_value=self._mock_run(
                    [self._make_gh_pr_json(number=10, head_ref_oid="sha_v1")]
                ),
            ):
                handler.handle(input_model)

            with patch(
                "subprocess.run",
                return_value=self._mock_run(
                    [self._make_gh_pr_json(number=10, head_ref_oid="sha_v2")]
                ),
            ):
                result2 = handler.handle(input_model)

            assert len(result2.stall_events) == 0
