"""HandlerBaselineCapture — captures named system state snapshots as baseline artifacts.

Runs configurable probes concurrently (GitHub PRs, Linear tickets, service health,
Kafka topics, git branches, DB row counts) and writes a JSON artifact to disk.
Probe failures are non-fatal: the snapshot is still written with whatever data
was collected, and the failed probes appear in ``result.probes_failed``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    BaselineProbeType,
    ModelBaselineSnapshot,
    ProbeSnapshotItem,
)

logger = logging.getLogger(__name__)

_DEFAULT_PROBES: list[str] = [
    BaselineProbeType.GITHUB_PRS,
    BaselineProbeType.LINEAR_TICKETS,
    BaselineProbeType.SYSTEM_HEALTH,
    BaselineProbeType.GIT_BRANCHES,
]

_DEFAULT_OUTPUT_BASE = ".onex_state/baselines"


# ---------------------------------------------------------------------------
# Probe protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ProbeProtocol(Protocol):
    """Protocol all baseline probes must implement."""

    name: str

    async def collect(self, omni_home: str) -> list[ProbeSnapshotItem]:
        """Collect a snapshot for this probe.

        Must never raise — return an empty list on any infrastructure failure.
        """
        ...


# ---------------------------------------------------------------------------
# Request / result models
# ---------------------------------------------------------------------------


class ModelBaselineCaptureRequest(BaseModel):
    """Input model for the baseline capture handler."""

    model_config = {"frozen": True, "extra": "forbid"}

    baseline_id: str = Field(..., description="Unique name for this baseline snapshot.")
    probes: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_PROBES),
        description=(
            "Probe names to run: github_prs, linear_tickets, system_health, "
            "kafka_topics, git_branches, db_row_counts"
        ),
    )
    label: str | None = Field(
        default=None, description="Human-readable label for this baseline."
    )
    omni_home: str = Field(
        default="/Volumes/PRO-G40/Code/omni_home",
        description="Root path of the omni_home workspace.",
    )
    output_path: str | None = Field(
        default=None,
        description=(
            "Override artifact output path. Defaults to "
            f"{_DEFAULT_OUTPUT_BASE}/{{baseline_id}}.json relative to omni_home."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="If true, capture and return snapshot without writing artifact.",
    )


class ModelBaselineCaptureResult(BaseModel):
    """Output model for the baseline capture handler."""

    model_config = {"frozen": True, "extra": "forbid"}

    baseline_id: str = Field(..., description="The baseline ID that was captured.")
    captured_at: datetime = Field(..., description="UTC timestamp of capture.")
    probes_run: list[str] = Field(
        default_factory=list, description="Probes that completed successfully."
    )
    probes_failed: list[str] = Field(
        default_factory=list, description="Probes that failed (non-fatal)."
    )
    artifact_path: str = Field(
        ...,
        description="Filesystem path where the baseline JSON was written (or would have been).",
    )
    dry_run: bool = Field(..., description="Whether this was a dry run.")
    snapshot: ModelBaselineSnapshot = Field(
        ..., description="The captured baseline snapshot."
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerBaselineCapture:
    """Captures named system state snapshots as baseline artifacts.

    Probes are run concurrently with ``asyncio.gather(return_exceptions=True)``.
    Each probe failure is recorded in ``result.probes_failed`` without aborting
    the capture. The resulting artifact is always valid JSON that can be
    deserialized back to ``ModelBaselineSnapshot``.

    Usage::

        handler = HandlerBaselineCapture()
        result = await handler.handle(ModelBaselineCaptureRequest(baseline_id="pre-deploy"))
    """

    def __init__(self, probe_registry: dict[str, ProbeProtocol] | None = None) -> None:
        """Initialise the handler.

        Args:
            probe_registry: Optional override of the probe registry. If ``None``,
                the handler imports and registers all built-in probes lazily on
                first ``handle()`` call.
        """
        self._probe_registry: dict[str, ProbeProtocol] | None = probe_registry

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_registry(self) -> dict[str, ProbeProtocol]:
        """Return the probe registry, building it lazily if needed."""
        if self._probe_registry is not None:
            return self._probe_registry

        # Lazy import so that probe modules' heavy deps are not loaded unless
        # the handler is actually used. type: ignore comments are needed here
        # because the probe modules are implemented in a parallel PR (OMN-7954)
        # and do not yet exist in the source tree at type-check time.
        from omnimarket.nodes.node_baseline_capture.handlers.probes import (  # type: ignore[attr-defined]
            probe_db_row_counts,
            probe_git_branches,
            probe_github_prs,
            probe_kafka_topics,
            probe_linear_tickets,
            probe_system_health,
        )

        registry: dict[str, ProbeProtocol] = {
            BaselineProbeType.GITHUB_PRS: probe_github_prs.ProbeGitHubPRs(),
            BaselineProbeType.LINEAR_TICKETS: probe_linear_tickets.ProbeLinearTickets(),
            BaselineProbeType.SYSTEM_HEALTH: probe_system_health.ProbeSystemHealth(),
            BaselineProbeType.KAFKA_TOPICS: probe_kafka_topics.ProbeKafkaTopics(),
            BaselineProbeType.GIT_BRANCHES: probe_git_branches.ProbeGitBranches(),
            BaselineProbeType.DB_ROW_COUNTS: probe_db_row_counts.ProbeDbRowCounts(),
        }
        self._probe_registry = registry
        return registry

    def _resolve_output_path(self, request: ModelBaselineCaptureRequest) -> Path:
        """Resolve the artifact output path from the request."""
        if request.output_path is not None:
            return Path(request.output_path)
        return (
            Path(request.omni_home)
            / _DEFAULT_OUTPUT_BASE
            / f"{request.baseline_id}.json"
        )

    async def _run_probe(
        self,
        probe: ProbeProtocol,
        omni_home: str,
    ) -> tuple[str, list[ProbeSnapshotItem] | Exception]:
        """Run a single probe and return (name, results_or_exception)."""
        try:
            items = await probe.collect(omni_home)
            return probe.name, items
        except Exception as exc:
            logger.warning("Probe %r failed: %s", probe.name, exc)
            return probe.name, exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def handle(
        self, request: ModelBaselineCaptureRequest
    ) -> ModelBaselineCaptureResult:
        """Execute the baseline capture.

        Args:
            request: Capture configuration.

        Returns:
            Result containing the snapshot, artifact path, and probe status.
        """
        registry = self._get_registry()
        captured_at = datetime.now(UTC)
        output_path = self._resolve_output_path(request)

        # Resolve probes — unknown names are logged and skipped
        probes_to_run: list[ProbeProtocol] = []
        for name in request.probes:
            probe = registry.get(name)
            if probe is None:
                logger.warning("Unknown probe %r — skipping", name)
            else:
                probes_to_run.append(probe)

        # Run all probes concurrently
        raw_results: list[Any] = await asyncio.gather(
            *[self._run_probe(p, request.omni_home) for p in probes_to_run],
            return_exceptions=True,
        )

        probes_run: list[str] = []
        probes_failed: list[str] = []
        probe_data: dict[str, list[ProbeSnapshotItem]] = {}

        for raw in raw_results:
            if isinstance(raw, Exception):
                # asyncio.gather itself raised (shouldn't happen with return_exceptions)
                logger.error("Unexpected gather exception: %s", raw)
                continue
            probe_name, result = raw
            if isinstance(result, Exception):
                probes_failed.append(probe_name)
            else:
                probes_run.append(probe_name)
                probe_data[probe_name] = result

        snapshot = ModelBaselineSnapshot(
            baseline_id=request.baseline_id,
            captured_at=captured_at,
            label=request.label,
            probes=probe_data,
        )

        # Write artifact unless dry_run
        if not request.dry_run:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.exists():
                logger.warning(
                    "Overwriting existing baseline artifact at %s", output_path
                )
            output_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
            logger.info(
                "Baseline %r captured: %d probes run, %d failed → %s",
                request.baseline_id,
                len(probes_run),
                len(probes_failed),
                output_path,
            )
        else:
            logger.info(
                "Baseline %r dry-run: %d probes run, %d failed (no artifact written)",
                request.baseline_id,
                len(probes_run),
                len(probes_failed),
            )

        return ModelBaselineCaptureResult(
            baseline_id=request.baseline_id,
            captured_at=captured_at,
            probes_run=probes_run,
            probes_failed=probes_failed,
            artifact_path=str(output_path),
            dry_run=request.dry_run,
            snapshot=snapshot,
        )


__all__: list[str] = [
    "HandlerBaselineCapture",
    "ModelBaselineCaptureRequest",
    "ModelBaselineCaptureResult",
    "ProbeProtocol",
]
