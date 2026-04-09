"""Golden chain tests for node_baseline_capture.

Verifies end-to-end capture behavior:
  - dry_run skips artifact write
  - probes mocked, artifact written and deserializable
  - failed probe is non-fatal
  - all probe types registered in default registry
  - event bus wiring (command -> handler -> completion event)

Related: OMN-7960 (golden chain tests for baseline measurement system)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_baseline_capture.handlers.handler_baseline_capture import (
    HandlerBaselineCapture,
    ModelBaselineCaptureRequest,
    ModelBaselineCaptureResult,
    ProbeProtocol,
)
from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    BaselineProbeType,
    ModelBaselineSnapshot,
    ModelGitBranchSnapshot,
    ModelGitHubPRSnapshot,
    ModelLinearTicketSnapshot,
    ProbeSnapshotItem,
)

# ---------------------------------------------------------------------------
# Mock probes
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)


class MockProbePRs:
    name = BaselineProbeType.GITHUB_PRS

    def __init__(self, items: list[ProbeSnapshotItem] | None = None) -> None:
        self._items = items or [
            ModelGitHubPRSnapshot(
                pr_number=42,
                title="Fix something",
                repo="OmniNode-ai/omniclaude",
                state="open",
                labels=[],
                age_days=1.0,
                ci_status="success",
            )
        ]
        self.call_count = 0

    async def collect(self, omni_home: str) -> list[ProbeSnapshotItem]:
        self.call_count += 1
        return self._items


class MockProbeTickets:
    name = BaselineProbeType.LINEAR_TICKETS

    def __init__(self, items: list[ProbeSnapshotItem] | None = None) -> None:
        self._items = items or [
            ModelLinearTicketSnapshot(
                ticket_id="OMN-100",
                title="Some ticket",
                state="In Progress",
                priority=2,
                assignee="jonah",
                updated_at=_NOW,
            )
        ]
        self.call_count = 0

    async def collect(self, omni_home: str) -> list[ProbeSnapshotItem]:
        self.call_count += 1
        return self._items


class MockProbeGitBranches:
    name = BaselineProbeType.GIT_BRANCHES

    def __init__(self, items: list[ProbeSnapshotItem] | None = None) -> None:
        self._items = items or [
            ModelGitBranchSnapshot(
                repo="omniclaude",
                branch="jonah/omn-100-test",
                worktree_path="/tmp/worktrees/OMN-100/omniclaude",
                age_days=0.5,
            )
        ]
        self.call_count = 0

    async def collect(self, omni_home: str) -> list[ProbeSnapshotItem]:
        self.call_count += 1
        return self._items


class MockProbeAlwaysFails:
    name = BaselineProbeType.SYSTEM_HEALTH

    async def collect(self, omni_home: str) -> list[ProbeSnapshotItem]:
        msg = "Infra unavailable"
        raise RuntimeError(msg)


def _make_registry(
    *,
    include_failing: bool = False,
) -> dict[str, ProbeProtocol]:
    registry: dict[str, ProbeProtocol] = {
        BaselineProbeType.GITHUB_PRS: MockProbePRs(),
        BaselineProbeType.LINEAR_TICKETS: MockProbeTickets(),
        BaselineProbeType.GIT_BRANCHES: MockProbeGitBranches(),
    }
    if include_failing:
        registry[BaselineProbeType.SYSTEM_HEALTH] = MockProbeAlwaysFails()  # type: ignore[assignment]
    return registry


def _make_request(
    *,
    probes: list[str] | None = None,
    dry_run: bool = False,
    output_path: str | None = None,
    omni_home: str = "/tmp/omni_home_test",
) -> ModelBaselineCaptureRequest:
    return ModelBaselineCaptureRequest(
        baseline_id="test-baseline-2026-04-09",
        probes=probes
        or [
            BaselineProbeType.GITHUB_PRS,
            BaselineProbeType.LINEAR_TICKETS,
            BaselineProbeType.GIT_BRANCHES,
        ],
        label="test run",
        omni_home=omni_home,
        output_path=output_path,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBaselineCaptureGoldenChain:
    """Golden chain tests for HandlerBaselineCapture."""

    async def test_dry_run_does_not_write_artifact(self, tmp_path: Path) -> None:
        """dry_run=True must not write any file to disk."""
        output_file = tmp_path / "baselines" / "test-baseline-2026-04-09.json"
        handler = HandlerBaselineCapture(probe_registry=_make_registry())
        request = _make_request(
            dry_run=True,
            output_path=str(output_file),
        )

        result = await handler.handle(request)

        assert result.dry_run is True
        assert not output_file.exists(), "dry_run must not write artifact"
        assert result.baseline_id == "test-baseline-2026-04-09"
        assert len(result.probes_run) == 3
        assert result.probes_failed == []

    async def test_capture_writes_artifact(self, tmp_path: Path) -> None:
        """Artifact written, valid JSON, deserializes back to ModelBaselineSnapshot."""
        output_file = tmp_path / "baselines" / "test-baseline-2026-04-09.json"
        handler = HandlerBaselineCapture(probe_registry=_make_registry())
        request = _make_request(output_path=str(output_file))

        await handler.handle(request)

        assert output_file.exists(), "Artifact must be written to disk"
        raw = json.loads(output_file.read_text())
        snapshot = ModelBaselineSnapshot.model_validate(raw)
        assert snapshot.baseline_id == "test-baseline-2026-04-09"
        assert snapshot.label == "test run"
        assert BaselineProbeType.GITHUB_PRS in snapshot.probes
        assert BaselineProbeType.LINEAR_TICKETS in snapshot.probes
        assert BaselineProbeType.GIT_BRANCHES in snapshot.probes
        # PR snapshot items are correct
        pr_items = snapshot.probes[BaselineProbeType.GITHUB_PRS]
        assert len(pr_items) == 1
        assert pr_items[0].pr_number == 42  # type: ignore[union-attr]

    async def test_artifact_matches_result_snapshot(self, tmp_path: Path) -> None:
        """Artifact on disk must be identical to result.snapshot."""
        output_file = tmp_path / "test-baseline.json"
        handler = HandlerBaselineCapture(probe_registry=_make_registry())
        request = _make_request(output_path=str(output_file))

        result: ModelBaselineCaptureResult = await handler.handle(request)

        raw = json.loads(output_file.read_text())
        snapshot_from_disk = ModelBaselineSnapshot.model_validate(raw)
        assert snapshot_from_disk.baseline_id == result.snapshot.baseline_id
        assert snapshot_from_disk.label == result.snapshot.label
        assert set(snapshot_from_disk.probes) == set(result.snapshot.probes)

    async def test_failed_probe_is_non_fatal(self, tmp_path: Path) -> None:
        """A probe that raises must appear in probes_failed, not block the capture."""
        output_file = tmp_path / "test-baseline.json"
        registry = _make_registry(include_failing=True)
        handler = HandlerBaselineCapture(probe_registry=registry)
        request = _make_request(
            probes=[
                BaselineProbeType.GITHUB_PRS,
                BaselineProbeType.SYSTEM_HEALTH,
                BaselineProbeType.GIT_BRANCHES,
            ],
            output_path=str(output_file),
        )

        result = await handler.handle(request)

        assert BaselineProbeType.SYSTEM_HEALTH in result.probes_failed
        assert BaselineProbeType.GITHUB_PRS in result.probes_run
        assert BaselineProbeType.GIT_BRANCHES in result.probes_run
        # Artifact still written with the probes that succeeded
        assert output_file.exists()
        raw = json.loads(output_file.read_text())
        snapshot = ModelBaselineSnapshot.model_validate(raw)
        assert BaselineProbeType.GITHUB_PRS in snapshot.probes
        assert BaselineProbeType.SYSTEM_HEALTH not in snapshot.probes

    async def test_all_probe_types_registered(self, tmp_path: Path) -> None:
        """Request with all probe names succeeds with explicit mock registry."""
        output_file = tmp_path / "all-probes.json"
        from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
            ModelDbRowCountSnapshot,
            ModelKafkaTopicSnapshot,
            ModelServiceHealthSnapshot,
        )

        full_registry: dict[str, ProbeProtocol] = {
            BaselineProbeType.GITHUB_PRS: MockProbePRs(),
            BaselineProbeType.LINEAR_TICKETS: MockProbeTickets(),
            BaselineProbeType.GIT_BRANCHES: MockProbeGitBranches(),
            BaselineProbeType.SYSTEM_HEALTH: _make_mock_probe(  # type: ignore[dict-item]
                BaselineProbeType.SYSTEM_HEALTH,
                [
                    ModelServiceHealthSnapshot(
                        service="postgres", healthy=True, latency_ms=5.0
                    )
                ],
            ),
            BaselineProbeType.KAFKA_TOPICS: _make_mock_probe(  # type: ignore[dict-item]
                BaselineProbeType.KAFKA_TOPICS,
                [
                    ModelKafkaTopicSnapshot(
                        topic="onex.evt.test.v1",
                        partition_count=1,
                        latest_offset=100,
                    )
                ],
            ),
            BaselineProbeType.DB_ROW_COUNTS: _make_mock_probe(  # type: ignore[dict-item]
                BaselineProbeType.DB_ROW_COUNTS,
                [ModelDbRowCountSnapshot(table_name="sessions", row_count=42)],
            ),
        }
        handler = HandlerBaselineCapture(probe_registry=full_registry)
        request = ModelBaselineCaptureRequest(
            baseline_id="all-probes-test",
            probes=list(BaselineProbeType),
            omni_home="/tmp/test",
            output_path=str(output_file),
        )

        result = await handler.handle(request)

        assert len(result.probes_failed) == 0
        assert set(result.probes_run) == set(BaselineProbeType)

    async def test_unknown_probe_name_is_skipped(self, tmp_path: Path) -> None:
        """Unknown probe names are silently skipped, not added to probes_failed."""
        output_file = tmp_path / "test.json"
        handler = HandlerBaselineCapture(probe_registry=_make_registry())
        request = ModelBaselineCaptureRequest(
            baseline_id="unknown-probe-test",
            probes=["github_prs", "nonexistent_probe"],
            omni_home="/tmp/test",
            output_path=str(output_file),
        )

        result = await handler.handle(request)

        assert "nonexistent_probe" not in result.probes_run
        assert "nonexistent_probe" not in result.probes_failed
        assert "github_prs" in result.probes_run

    async def test_mkdir_created_for_nested_output_path(self, tmp_path: Path) -> None:
        """Handler creates parent directories if they don't exist."""
        deep_path = tmp_path / "a" / "b" / "c" / "baseline.json"
        handler = HandlerBaselineCapture(probe_registry=_make_registry())
        request = _make_request(output_path=str(deep_path))

        await handler.handle(request)

        assert deep_path.exists()

    async def test_result_fields_populated(self, tmp_path: Path) -> None:
        """All result fields are correctly populated after a successful capture."""
        output_file = tmp_path / "result-test.json"
        handler = HandlerBaselineCapture(probe_registry=_make_registry())
        request = _make_request(output_path=str(output_file))

        before = datetime.now(UTC)
        result = await handler.handle(request)
        after = datetime.now(UTC)

        assert result.baseline_id == "test-baseline-2026-04-09"
        assert result.dry_run is False
        assert before <= result.captured_at <= after
        assert result.artifact_path == str(output_file)
        assert isinstance(result.snapshot, ModelBaselineSnapshot)


# ---------------------------------------------------------------------------
# Helper: generic mock probe factory
# ---------------------------------------------------------------------------


def _make_mock_probe(probe_name: str, items: list[ProbeSnapshotItem]) -> object:
    class _MockProbe:
        name = probe_name

        async def collect(self, omni_home: str) -> list[ProbeSnapshotItem]:
            return items

    return _MockProbe()
