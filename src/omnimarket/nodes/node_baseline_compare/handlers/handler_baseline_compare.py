"""HandlerBaselineCompare — diffs current state against a previously captured baseline.

Loads a named baseline artifact from disk, re-runs the same probes to capture current
state, computes per-probe deltas, writes a delta JSON artifact, and returns a human-
readable summary. Missing baseline artifacts are handled gracefully without raising.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from omnimarket.nodes.node_baseline_capture.handlers.handler_baseline_capture import (
    HandlerBaselineCapture,
    ModelBaselineCaptureRequest,
    ProbeProtocol,
)
from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    BaselineProbeType,
    ModelBaselineDelta,
    ModelBaselineSnapshot,
    ModelDbRowCountDelta,
    ModelDbRowCountSnapshot,
    ModelGitBranchDelta,
    ModelGitBranchSnapshot,
    ModelGitHubPRDelta,
    ModelGitHubPRSnapshot,
    ModelKafkaTopicDelta,
    ModelKafkaTopicSnapshot,
    ModelLinearTicketDelta,
    ModelLinearTicketSnapshot,
    ModelServiceHealthDelta,
    ModelServiceHealthSnapshot,
    ProbeSnapshotItem,
)

logger = logging.getLogger(__name__)

_DEFAULT_OUTPUT_BASE = ".onex_state/baselines"


# ---------------------------------------------------------------------------
# Request / result models
# ---------------------------------------------------------------------------


class ModelBaselineCompareRequest(BaseModel):
    """Input model for the baseline compare handler."""

    model_config = {"frozen": True, "extra": "forbid"}

    baseline_id: str = Field(
        ..., description="Name of the baseline artifact to compare against."
    )
    probes: list[str] | None = Field(
        default=None,
        description="Probe names to compare. None = compare all probes present in baseline.",
    )
    omni_home: str = Field(
        default="/Volumes/PRO-G40/Code/omni_home",
        description="Root path of the omni_home workspace.",
    )
    baseline_path: str | None = Field(
        default=None,
        description="Override artifact lookup path.",
    )
    current_snapshot: ModelBaselineSnapshot | None = Field(
        default=None,
        description="Pre-captured current snapshot. If provided, skips re-probing.",
    )
    dry_run: bool = Field(
        default=False,
        description="If true, compute delta without writing delta artifact.",
    )


class ModelBaselineCompareResult(BaseModel):
    """Output model for the baseline compare handler."""

    model_config = {"frozen": True, "extra": "forbid"}

    baseline_id: str = Field(..., description="The baseline ID that was compared.")
    baseline_captured_at: datetime = Field(
        ..., description="UTC timestamp of the original baseline."
    )
    compared_at: datetime = Field(
        ..., description="UTC timestamp when comparison was run."
    )
    delta: ModelBaselineDelta = Field(..., description="Computed per-probe deltas.")
    summary: str = Field(..., description="One-paragraph human-readable summary.")
    report_path: str = Field(
        ..., description="Path where delta JSON was written (or would be)."
    )
    dry_run: bool = Field(..., description="Whether this was a dry run.")
    error: str | None = Field(
        default=None, description="Error message if baseline load failed."
    )


# ---------------------------------------------------------------------------
# Delta computation helpers
# ---------------------------------------------------------------------------


def _compute_github_pr_delta(
    before: list[ProbeSnapshotItem],
    after: list[ProbeSnapshotItem],
) -> ModelGitHubPRDelta:
    before_prs = {
        p.pr_number: p for p in before if isinstance(p, ModelGitHubPRSnapshot)
    }
    after_prs = {p.pr_number: p for p in after if isinstance(p, ModelGitHubPRSnapshot)}

    before_nums = set(before_prs)
    after_nums = set(after_prs)

    opened = sorted(after_nums - before_nums)
    disappeared = before_nums - after_nums

    merged: list[int] = []
    closed: list[int] = []
    track_changes: dict[int, str] = {}

    for num in sorted(disappeared):
        pr = before_prs[num]
        if pr.state == "merged":
            merged.append(num)
        else:
            closed.append(num)

    # State changes for PRs present in both snapshots
    for num in sorted(before_nums & after_nums):
        b = before_prs[num]
        a = after_prs[num]
        if b.state != a.state:
            track_changes[num] = f"{b.state} -> {a.state}"

    return ModelGitHubPRDelta(
        opened=opened,
        closed=closed,
        merged=merged,
        track_changes=track_changes,
    )


def _compute_linear_ticket_delta(
    before: list[ProbeSnapshotItem],
    after: list[ProbeSnapshotItem],
) -> ModelLinearTicketDelta:
    before_tickets = {
        t.ticket_id: t for t in before if isinstance(t, ModelLinearTicketSnapshot)
    }
    after_tickets = {
        t.ticket_id: t for t in after if isinstance(t, ModelLinearTicketSnapshot)
    }

    before_ids = set(before_tickets)
    after_ids = set(after_tickets)

    opened = sorted(after_ids - before_ids)
    disappeared = sorted(before_ids - after_ids)

    _done_states = {"done", "cancelled", "canceled", "completed", "closed"}
    closed_done = [
        tid for tid in disappeared if before_tickets[tid].state.lower() in _done_states
    ]

    state_changes: dict[str, str] = {}
    for tid in sorted(before_ids & after_ids):
        b = before_tickets[tid]
        a = after_tickets[tid]
        if b.state != a.state:
            state_changes[tid] = f"{b.state} -> {a.state}"

    return ModelLinearTicketDelta(
        opened=opened,
        closed_done=closed_done,
        state_changes=state_changes,
    )


def _compute_service_health_delta(
    before: list[ProbeSnapshotItem],
    after: list[ProbeSnapshotItem],
) -> ModelServiceHealthDelta:
    before_map = {
        s.service: s for s in before if isinstance(s, ModelServiceHealthSnapshot)
    }
    after_map = {
        s.service: s for s in after if isinstance(s, ModelServiceHealthSnapshot)
    }

    recovered: list[str] = []
    degraded: list[str] = []
    new_failures: list[str] = []

    for svc, a in after_map.items():
        if svc not in before_map:
            if not a.healthy:
                new_failures.append(svc)
        else:
            b = before_map[svc]
            if not b.healthy and a.healthy:
                recovered.append(svc)
            elif b.healthy and not a.healthy:
                degraded.append(svc)

    return ModelServiceHealthDelta(
        recovered=sorted(recovered),
        degraded=sorted(degraded),
        new_failures=sorted(new_failures),
    )


def _compute_kafka_topic_delta(
    before: list[ProbeSnapshotItem],
    after: list[ProbeSnapshotItem],
) -> ModelKafkaTopicDelta:
    before_map = {t.topic: t for t in before if isinstance(t, ModelKafkaTopicSnapshot)}
    after_map = {t.topic: t for t in after if isinstance(t, ModelKafkaTopicSnapshot)}

    created = sorted(set(after_map) - set(before_map))
    deleted = sorted(set(before_map) - set(after_map))
    offset_advances: dict[str, int] = {}

    for topic in sorted(set(before_map) & set(after_map)):
        delta = after_map[topic].latest_offset - before_map[topic].latest_offset
        if delta != 0:
            offset_advances[topic] = delta

    return ModelKafkaTopicDelta(
        created=created, deleted=deleted, offset_advances=offset_advances
    )


def _compute_git_branch_delta(
    before: list[ProbeSnapshotItem],
    after: list[ProbeSnapshotItem],
) -> ModelGitBranchDelta:
    before_branches = {
        b.branch for b in before if isinstance(b, ModelGitBranchSnapshot)
    }
    after_branches = {b.branch for b in after if isinstance(b, ModelGitBranchSnapshot)}

    merged = sorted(before_branches - after_branches)
    created = sorted(after_branches - before_branches)

    return ModelGitBranchDelta(merged=merged, created=created, stale=[])


def _compute_db_row_count_delta(
    before: list[ProbeSnapshotItem],
    after: list[ProbeSnapshotItem],
) -> ModelDbRowCountDelta:
    before_map = {
        r.table_name: r.row_count
        for r in before
        if isinstance(r, ModelDbRowCountSnapshot)
    }
    after_map = {
        r.table_name: r.row_count
        for r in after
        if isinstance(r, ModelDbRowCountSnapshot)
    }

    grown: list[str] = []
    shrunk: list[str] = []
    unchanged: list[str] = []
    row_delta_by_table: dict[str, int] = {}

    all_tables = sorted(set(before_map) | set(after_map))
    for table in all_tables:
        b_count = before_map.get(table, 0)
        a_count = after_map.get(table, 0)
        delta = a_count - b_count
        row_delta_by_table[table] = delta
        if delta > 0:
            grown.append(table)
        elif delta < 0:
            shrunk.append(table)
        else:
            unchanged.append(table)

    return ModelDbRowCountDelta(
        grown=grown,
        shrunk=shrunk,
        unchanged=unchanged,
        row_delta_by_table=row_delta_by_table,
    )


_DELTA_COMPUTERS: dict[str, Any] = {
    BaselineProbeType.GITHUB_PRS: _compute_github_pr_delta,
    BaselineProbeType.LINEAR_TICKETS: _compute_linear_ticket_delta,
    BaselineProbeType.SYSTEM_HEALTH: _compute_service_health_delta,
    BaselineProbeType.KAFKA_TOPICS: _compute_kafka_topic_delta,
    BaselineProbeType.GIT_BRANCHES: _compute_git_branch_delta,
    BaselineProbeType.DB_ROW_COUNTS: _compute_db_row_count_delta,
}


def _generate_summary(delta: ModelBaselineDelta) -> str:
    parts: list[str] = [
        f"Baseline '{delta.baseline_id}' compared at {delta.compared_at.isoformat()}."
    ]
    for probe_name, probe_delta in delta.per_probe_deltas.items():
        if isinstance(probe_delta, ModelGitHubPRDelta):
            parts.append(
                f"GitHub PRs: {len(probe_delta.opened)} opened, "
                f"{len(probe_delta.merged)} merged, {len(probe_delta.closed)} closed."
            )
        elif isinstance(probe_delta, ModelLinearTicketDelta):
            parts.append(
                f"Linear tickets: {len(probe_delta.opened)} opened, "
                f"{len(probe_delta.closed_done)} closed/done, "
                f"{len(probe_delta.state_changes)} state changes."
            )
        elif isinstance(probe_delta, ModelServiceHealthDelta):
            parts.append(
                f"Service health: {len(probe_delta.recovered)} recovered, "
                f"{len(probe_delta.degraded)} degraded."
            )
        elif isinstance(probe_delta, ModelKafkaTopicDelta):
            parts.append(
                f"Kafka topics: {len(probe_delta.created)} created, "
                f"{len(probe_delta.deleted)} deleted."
            )
        elif isinstance(probe_delta, ModelGitBranchDelta):
            parts.append(
                f"Git branches: {len(probe_delta.created)} created, "
                f"{len(probe_delta.merged)} merged/deleted."
            )
        elif isinstance(probe_delta, ModelDbRowCountDelta):
            parts.append(
                f"DB row counts: {len(probe_delta.grown)} tables grew, "
                f"{len(probe_delta.shrunk)} shrunk."
            )
        else:
            parts.append(f"Probe '{probe_name}': delta computed.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerBaselineCompare:
    """Diffs current system state against a previously captured baseline artifact.

    Usage::

        handler = HandlerBaselineCompare()
        result = await handler.handle(ModelBaselineCompareRequest(baseline_id="pre-deploy"))
    """

    def __init__(self, probe_registry: dict[str, ProbeProtocol] | None = None) -> None:
        self._capture_handler = HandlerBaselineCapture(probe_registry=probe_registry)

    def _resolve_baseline_path(self, request: ModelBaselineCompareRequest) -> Path:
        if request.baseline_path is not None:
            return Path(request.baseline_path)
        return (
            Path(request.omni_home)
            / _DEFAULT_OUTPUT_BASE
            / f"{request.baseline_id}.json"
        )

    def _resolve_report_path(self, request: ModelBaselineCompareRequest) -> Path:
        return (
            Path(request.omni_home)
            / _DEFAULT_OUTPUT_BASE
            / f"{request.baseline_id}.delta.json"
        )

    async def handle(
        self, request: ModelBaselineCompareRequest
    ) -> ModelBaselineCompareResult:
        compared_at = datetime.now(UTC)
        report_path = self._resolve_report_path(request)

        # --- Load baseline artifact ---
        baseline_path = self._resolve_baseline_path(request)
        if not baseline_path.exists():
            error_msg = f"Baseline artifact not found: {baseline_path}"
            logger.error(error_msg)
            # Return graceful error result — do not raise
            return ModelBaselineCompareResult(
                baseline_id=request.baseline_id,
                baseline_captured_at=compared_at,
                compared_at=compared_at,
                delta=ModelBaselineDelta(
                    baseline_id=request.baseline_id,
                    baseline_captured_at=compared_at,
                    compared_at=compared_at,
                ),
                summary=f"ERROR: {error_msg}",
                report_path=str(report_path),
                dry_run=request.dry_run,
                error=error_msg,
            )

        try:
            raw = json.loads(baseline_path.read_text(encoding="utf-8"))
            baseline_snapshot = ModelBaselineSnapshot.model_validate(raw)
        except Exception as exc:
            error_msg = f"Failed to parse baseline artifact at {baseline_path}: {exc}"
            logger.error(error_msg)
            return ModelBaselineCompareResult(
                baseline_id=request.baseline_id,
                baseline_captured_at=compared_at,
                compared_at=compared_at,
                delta=ModelBaselineDelta(
                    baseline_id=request.baseline_id,
                    baseline_captured_at=compared_at,
                    compared_at=compared_at,
                ),
                summary=f"ERROR: {error_msg}",
                report_path=str(report_path),
                dry_run=request.dry_run,
                error=error_msg,
            )

        # --- Determine which probes to compare ---
        probes_in_baseline = list(baseline_snapshot.probes.keys())
        probes_to_compare = (
            request.probes if request.probes is not None else probes_in_baseline
        )

        # --- Capture current state (or use provided snapshot) ---
        if request.current_snapshot is not None:
            current_snapshot = request.current_snapshot
        else:
            capture_request = ModelBaselineCaptureRequest(
                baseline_id=f"{request.baseline_id}__current",
                probes=probes_to_compare,
                omni_home=request.omni_home,
                dry_run=True,  # Don't write the current snapshot
            )
            capture_result = await self._capture_handler.handle(capture_request)
            current_snapshot = capture_result.snapshot

        # --- Compute deltas per probe ---
        per_probe_deltas = {}
        for probe_name in probes_to_compare:
            if probe_name not in _DELTA_COMPUTERS:
                logger.warning("No delta computer for probe %r — skipping", probe_name)
                continue
            before_items = baseline_snapshot.probes.get(probe_name, [])
            after_items = current_snapshot.probes.get(probe_name, [])
            try:
                per_probe_deltas[probe_name] = _DELTA_COMPUTERS[probe_name](
                    before_items, after_items
                )
            except Exception as exc:
                logger.warning(
                    "Delta computation failed for probe %r: %s", probe_name, exc
                )

        delta = ModelBaselineDelta(
            baseline_id=request.baseline_id,
            baseline_captured_at=baseline_snapshot.captured_at,
            compared_at=compared_at,
            per_probe_deltas=per_probe_deltas,
        )
        summary = _generate_summary(delta)

        # --- Write delta artifact unless dry_run ---
        if not request.dry_run:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(delta.model_dump_json(indent=2), encoding="utf-8")
            logger.info(
                "Baseline %r compare complete → %s",
                request.baseline_id,
                report_path,
            )

        return ModelBaselineCompareResult(
            baseline_id=request.baseline_id,
            baseline_captured_at=baseline_snapshot.captured_at,
            compared_at=compared_at,
            delta=delta,
            summary=summary,
            report_path=str(report_path),
            dry_run=request.dry_run,
            error=None,
        )


__all__: list[str] = [
    "HandlerBaselineCompare",
    "ModelBaselineCompareRequest",
    "ModelBaselineCompareResult",
]
