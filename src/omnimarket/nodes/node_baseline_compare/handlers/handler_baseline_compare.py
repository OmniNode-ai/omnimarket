# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerBaselineCompare — diffs current system state against a captured baseline.

Loads a baseline artifact from disk, re-runs the same probes to capture current
state, computes per-probe deltas, writes a delta artifact, and returns a
human-readable summary.

Probe failures during re-capture are non-fatal: the delta is still produced
for all probes that succeeded.
"""

from __future__ import annotations

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
    ProbeDeltaItem,
    ProbeSnapshotItem,
)

logger = logging.getLogger(__name__)

_DEFAULT_OUTPUT_BASE = ".onex_state/baselines"

_STALE_BRANCH_DAYS = 14.0  # branches older than this are marked stale


# ---------------------------------------------------------------------------
# Request / result models
# ---------------------------------------------------------------------------


class ModelBaselineCompareRequest(BaseModel):
    """Input model for the baseline compare handler."""

    model_config = {"frozen": True, "extra": "forbid"}

    baseline_id: str = Field(
        ..., description="Baseline ID to compare against (artifact must exist on disk)."
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
        description="Override artifact path. Defaults to .onex_state/baselines/{baseline_id}.json.",
    )
    current_snapshot: ModelBaselineSnapshot | None = Field(
        default=None,
        description=(
            "Pre-captured current snapshot. If provided, probes are not re-run. "
            "Useful for capture-then-compare without a second network round-trip."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="If true, compute delta but do not write delta artifact to disk.",
    )


class ModelBaselineCompareResult(BaseModel):
    """Output model for the baseline compare handler."""

    model_config = {"frozen": True, "extra": "forbid"}

    baseline_id: str = Field(..., description="Baseline ID that was compared against.")
    baseline_captured_at: datetime = Field(
        ..., description="UTC timestamp of the original baseline."
    )
    compared_at: datetime = Field(
        ..., description="UTC timestamp when the comparison was run."
    )
    delta: ModelBaselineDelta = Field(..., description="Per-probe delta.")
    summary: str = Field(..., description="Human-readable 1-paragraph summary.")
    report_path: str = Field(
        ..., description="Path where delta JSON was written (or would have been)."
    )
    dry_run: bool = Field(..., description="Whether this was a dry run.")
    error: str | None = Field(
        default=None,
        description="Set if the handler encountered a fatal error (e.g. missing artifact).",
    )


# ---------------------------------------------------------------------------
# Per-probe diff functions
# ---------------------------------------------------------------------------


def _diff_github_prs(
    before: list[ProbeSnapshotItem], after: list[ProbeSnapshotItem]
) -> ModelGitHubPRDelta:
    b = {
        pr.pr_number: pr
        for pr in before
        if isinstance(pr, ModelGitHubPRSnapshot)
    }
    a = {
        pr.pr_number: pr
        for pr in after
        if isinstance(pr, ModelGitHubPRSnapshot)
    }
    before_nums = set(b)
    after_nums = set(a)

    opened = sorted(after_nums - before_nums)
    gone = sorted(before_nums - after_nums)

    merged: list[int] = []
    closed: list[int] = []
    track_changes: dict[int, str] = {}

    for num in gone:
        pr_before = b[num]
        if pr_before.state.upper() in {"MERGED", "merged"}:
            merged.append(num)
        else:
            closed.append(num)

    # Track state changes for PRs present in both
    for num in before_nums & after_nums:
        pb, pa = b[num], a[num]
        if pb.state != pa.state:
            track_changes[num] = f"{pb.state} -> {pa.state}"
        elif pb.ci_status != pa.ci_status:
            track_changes[num] = f"ci: {pb.ci_status} -> {pa.ci_status}"

    return ModelGitHubPRDelta(
        opened=opened, closed=closed, merged=merged, track_changes=track_changes
    )


def _diff_linear_tickets(
    before: list[ProbeSnapshotItem], after: list[ProbeSnapshotItem]
) -> ModelLinearTicketDelta:
    b = {
        t.ticket_id: t
        for t in before
        if isinstance(t, ModelLinearTicketSnapshot)
    }
    a = {
        t.ticket_id: t
        for t in after
        if isinstance(t, ModelLinearTicketSnapshot)
    }
    before_ids = set(b)
    after_ids = set(a)

    opened = sorted(after_ids - before_ids)
    gone = sorted(before_ids - after_ids)

    _done_states = {"Done", "Cancelled", "Completed", "Canceled"}
    closed_done = [tid for tid in gone if b[tid].state in _done_states]

    state_changes: dict[str, str] = {}
    for tid in before_ids & after_ids:
        tb, ta = b[tid], a[tid]
        if tb.state != ta.state:
            state_changes[tid] = f"{tb.state} -> {ta.state}"

    return ModelLinearTicketDelta(
        opened=opened, closed_done=closed_done, state_changes=state_changes
    )


def _diff_system_health(
    before: list[ProbeSnapshotItem], after: list[ProbeSnapshotItem]
) -> ModelServiceHealthDelta:
    b = {
        s.service: s
        for s in before
        if isinstance(s, ModelServiceHealthSnapshot)
    }
    a = {
        s.service: s
        for s in after
        if isinstance(s, ModelServiceHealthSnapshot)
    }
    before_svcs = set(b)
    after_svcs = set(a)

    new_failures = [
        svc for svc in after_svcs - before_svcs if not a[svc].healthy
    ]
    recovered: list[str] = []
    degraded: list[str] = []

    for svc in before_svcs & after_svcs:
        sb, sa = b[svc], a[svc]
        if not sb.healthy and sa.healthy:
            recovered.append(svc)
        elif sb.healthy and not sa.healthy:
            degraded.append(svc)

    return ModelServiceHealthDelta(
        recovered=sorted(recovered),
        degraded=sorted(degraded),
        new_failures=sorted(new_failures),
    )


def _diff_kafka_topics(
    before: list[ProbeSnapshotItem], after: list[ProbeSnapshotItem]
) -> ModelKafkaTopicDelta:
    b = {
        t.topic: t
        for t in before
        if isinstance(t, ModelKafkaTopicSnapshot)
    }
    a = {
        t.topic: t
        for t in after
        if isinstance(t, ModelKafkaTopicSnapshot)
    }
    before_topics = set(b)
    after_topics = set(a)

    created = sorted(after_topics - before_topics)
    deleted = sorted(before_topics - after_topics)
    offset_advances: dict[str, int] = {}

    for topic in before_topics & after_topics:
        delta = a[topic].latest_offset - b[topic].latest_offset
        if delta != 0:
            offset_advances[topic] = delta

    return ModelKafkaTopicDelta(
        created=created, deleted=deleted, offset_advances=offset_advances
    )


def _diff_git_branches(
    before: list[ProbeSnapshotItem], after: list[ProbeSnapshotItem]
) -> ModelGitBranchDelta:
    def _key(br: ModelGitBranchSnapshot) -> str:
        return f"{br.repo}::{br.branch}"

    b_map = {
        _key(br): br
        for br in before
        if isinstance(br, ModelGitBranchSnapshot)
    }
    a_map = {
        _key(br): br
        for br in after
        if isinstance(br, ModelGitBranchSnapshot)
    }
    before_keys = set(b_map)
    after_keys = set(a_map)

    merged = sorted(before_keys - after_keys)
    created = sorted(after_keys - before_keys)
    stale = sorted(
        key for key, br in a_map.items() if br.age_days >= _STALE_BRANCH_DAYS
    )

    return ModelGitBranchDelta(merged=merged, created=created, stale=stale)


def _diff_db_row_counts(
    before: list[ProbeSnapshotItem], after: list[ProbeSnapshotItem]
) -> ModelDbRowCountDelta:
    b = {
        r.table_name: r.row_count
        for r in before
        if isinstance(r, ModelDbRowCountSnapshot)
    }
    a = {
        r.table_name: r.row_count
        for r in after
        if isinstance(r, ModelDbRowCountSnapshot)
    }
    all_tables = set(b) | set(a)

    grown: list[str] = []
    shrunk: list[str] = []
    unchanged: list[str] = []
    row_delta_by_table: dict[str, int] = {}

    for table in sorted(all_tables):
        bc = b.get(table, 0)
        ac = a.get(table, 0)
        delta = ac - bc
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


_DIFF_DISPATCH: dict[
    str,
    Any,
] = {
    "github_prs": _diff_github_prs,
    "linear_tickets": _diff_linear_tickets,
    "system_health": _diff_system_health,
    "kafka_topics": _diff_kafka_topics,
    "git_branches": _diff_git_branches,
    "db_row_counts": _diff_db_row_counts,
}


# ---------------------------------------------------------------------------
# Summary generation
# ---------------------------------------------------------------------------


def _build_summary(delta: ModelBaselineDelta, baseline_id: str) -> str:
    """Generate a concise human-readable summary paragraph."""
    lines: list[str] = [
        f"Baseline '{baseline_id}' comparison "
        f"(captured {delta.baseline_captured_at.strftime('%Y-%m-%d %H:%M UTC')}, "
        f"compared {delta.compared_at.strftime('%Y-%m-%d %H:%M UTC')})."
    ]
    parts: list[str] = []

    for probe_name, probe_delta in delta.per_probe_deltas.items():
        if isinstance(probe_delta, ModelGitHubPRDelta):
            opened = len(probe_delta.opened)
            closed = len(probe_delta.closed)
            merged = len(probe_delta.merged)
            if opened or closed or merged:
                parts.append(
                    f"GitHub PRs: {opened} opened, {merged} merged, {closed} closed."
                )
        elif isinstance(probe_delta, ModelLinearTicketDelta):
            opened = len(probe_delta.opened)
            done = len(probe_delta.closed_done)
            changed = len(probe_delta.state_changes)
            if opened or done or changed:
                parts.append(
                    f"Linear tickets: {opened} new, {done} done/cancelled, "
                    f"{changed} state changes."
                )
        elif isinstance(probe_delta, ModelServiceHealthDelta):
            degraded = len(probe_delta.degraded)
            recovered = len(probe_delta.recovered)
            new_fail = len(probe_delta.new_failures)
            if degraded:
                parts.append(f"Service health: {degraded} degraded ({', '.join(probe_delta.degraded)}).")
            if recovered:
                parts.append(f"Service health: {recovered} recovered.")
            if new_fail:
                parts.append(f"Service health: {new_fail} new failures.")
            if not (degraded or recovered or new_fail):
                parts.append("Service health: no changes.")
        elif isinstance(probe_delta, ModelKafkaTopicDelta):
            created = len(probe_delta.created)
            deleted = len(probe_delta.deleted)
            advanced = len(probe_delta.offset_advances)
            if created or deleted:
                parts.append(
                    f"Kafka topics: {created} created, {deleted} deleted, "
                    f"{advanced} topics with new messages."
                )
        elif isinstance(probe_delta, ModelGitBranchDelta):
            merged = len(probe_delta.merged)
            created = len(probe_delta.created)
            stale = len(probe_delta.stale)
            if merged or created or stale:
                parts.append(
                    f"Git branches: {merged} merged, {created} new, {stale} stale."
                )
        elif isinstance(probe_delta, ModelDbRowCountDelta):
            grown = len(probe_delta.grown)
            shrunk = len(probe_delta.shrunk)
            if grown or shrunk:
                parts.append(
                    f"DB row counts: {grown} tables grew, {shrunk} tables shrank."
                )

    if parts:
        lines.extend(parts)
    else:
        lines.append("No significant changes detected across all probes.")

    return " ".join(lines)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerBaselineCompare:
    """Diffs current system state against a previously captured baseline.

    Usage::

        handler = HandlerBaselineCompare()
        result = await handler.handle(
            ModelBaselineCompareRequest(baseline_id="pre-deploy")
        )
        print(result.summary)
    """

    def __init__(self, probe_registry: dict[str, ProbeProtocol] | None = None) -> None:
        self._probe_registry = probe_registry

    def _resolve_baseline_path(self, request: ModelBaselineCompareRequest) -> Path:
        if request.baseline_path is not None:
            return Path(request.baseline_path)
        return (
            Path(request.omni_home)
            / _DEFAULT_OUTPUT_BASE
            / f"{request.baseline_id}.json"
        )

    def _resolve_delta_path(self, request: ModelBaselineCompareRequest) -> Path:
        return (
            Path(request.omni_home)
            / _DEFAULT_OUTPUT_BASE
            / f"{request.baseline_id}.delta.json"
        )

    async def _capture_current(
        self,
        baseline: ModelBaselineSnapshot,
        request: ModelBaselineCompareRequest,
    ) -> ModelBaselineSnapshot:
        """Re-run the probes from the baseline to get a current snapshot."""
        probe_names = list(request.probes or baseline.probes.keys())
        capture_handler = HandlerBaselineCapture(
            probe_registry=self._probe_registry
        )
        capture_request = ModelBaselineCaptureRequest(
            baseline_id=f"{request.baseline_id}__current",
            probes=probe_names,
            omni_home=request.omni_home,
            dry_run=True,  # don't write capture artifact — we only need the snapshot
        )
        capture_result = await capture_handler.handle(capture_request)
        return capture_result.snapshot

    async def handle(
        self, request: ModelBaselineCompareRequest
    ) -> ModelBaselineCompareResult:
        """Execute the baseline comparison."""
        compared_at = datetime.now(UTC)
        baseline_path = self._resolve_baseline_path(request)
        delta_path = self._resolve_delta_path(request)

        # 1. Load baseline artifact
        if not baseline_path.exists():
            logger.error("Baseline artifact not found: %s", baseline_path)
            # Return error result without raising
            compared_at_dt = datetime.now(UTC)
            error_delta = ModelBaselineDelta(
                baseline_id=request.baseline_id,
                baseline_captured_at=compared_at_dt,
                compared_at=compared_at_dt,
            )
            return ModelBaselineCompareResult(
                baseline_id=request.baseline_id,
                baseline_captured_at=compared_at_dt,
                compared_at=compared_at_dt,
                delta=error_delta,
                summary=f"Error: baseline artifact not found at {baseline_path}",
                report_path=str(delta_path),
                dry_run=request.dry_run,
                error=f"Baseline artifact not found: {baseline_path}",
            )

        try:
            baseline_json = baseline_path.read_text(encoding="utf-8")
            baseline = ModelBaselineSnapshot.model_validate_json(baseline_json)
        except Exception as exc:
            logger.error("Failed to load baseline artifact %s: %s", baseline_path, exc)
            compared_at_dt = datetime.now(UTC)
            error_delta = ModelBaselineDelta(
                baseline_id=request.baseline_id,
                baseline_captured_at=compared_at_dt,
                compared_at=compared_at_dt,
            )
            return ModelBaselineCompareResult(
                baseline_id=request.baseline_id,
                baseline_captured_at=compared_at_dt,
                compared_at=compared_at_dt,
                delta=error_delta,
                summary=f"Error: failed to parse baseline artifact: {exc}",
                report_path=str(delta_path),
                dry_run=request.dry_run,
                error=str(exc),
            )

        # 2. Get current snapshot (use provided or re-capture)
        if request.current_snapshot is not None:
            current = request.current_snapshot
        else:
            current = await self._capture_current(baseline, request)

        # 3. Compute per-probe deltas
        probe_names_to_compare = list(request.probes or baseline.probes.keys())
        per_probe_deltas: dict[str, ProbeDeltaItem] = {}

        for probe_name in probe_names_to_compare:
            diff_fn = _DIFF_DISPATCH.get(probe_name)
            if diff_fn is None:
                logger.warning("No diff function for probe %r — skipping", probe_name)
                continue
            before_items = baseline.probes.get(probe_name, [])
            after_items = current.probes.get(probe_name, [])
            try:
                probe_delta = diff_fn(before_items, after_items)
                per_probe_deltas[probe_name] = probe_delta
            except Exception as exc:
                logger.warning("Diff failed for probe %r: %s", probe_name, exc)

        # 4. Assemble delta
        delta = ModelBaselineDelta(
            baseline_id=request.baseline_id,
            baseline_captured_at=baseline.captured_at,
            compared_at=compared_at,
            per_probe_deltas=per_probe_deltas,
        )

        # 5. Generate summary
        summary = _build_summary(delta, request.baseline_id)

        # 6. Write delta artifact unless dry_run
        if not request.dry_run:
            delta_path.parent.mkdir(parents=True, exist_ok=True)
            delta_path.write_text(delta.model_dump_json(indent=2), encoding="utf-8")
            logger.info(
                "Baseline delta for %r written to %s",
                request.baseline_id,
                delta_path,
            )

        return ModelBaselineCompareResult(
            baseline_id=request.baseline_id,
            baseline_captured_at=baseline.captured_at,
            compared_at=compared_at,
            delta=delta,
            summary=summary,
            report_path=str(delta_path),
            dry_run=request.dry_run,
            error=None,
        )


__all__: list[str] = [
    "HandlerBaselineCompare",
    "ModelBaselineCompareRequest",
    "ModelBaselineCompareResult",
]
