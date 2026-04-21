# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrSnapshot — scans GitHub repos for open PRs via gh CLI.

This is an EFFECT handler — performs external I/O (subprocess calls to gh).
Produces ModelPRInfo objects compatible with ModelMergeSweepRequest for
direct wiring into the merge sweep pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from omnimarket.nodes.node_merge_sweep_compute.handlers.handler_merge_sweep import (
    ModelPRInfo,
)
from omnimarket.nodes.node_pr_snapshot_effect.models.model_pr_snapshot_input import (
    ModelPrSnapshotInput,
)
from omnimarket.nodes.node_pr_snapshot_effect.models.model_pr_snapshot_result import (
    ModelPrSnapshotResult,
)
from omnimarket.nodes.node_pr_snapshot_effect.models.model_pr_stall_event import (
    ModelPrStallEvent,
)
from omnimarket.nodes.node_pr_snapshot_effect.models.model_repo_scan_result import (
    ModelRepoScanResult,
)

logger = logging.getLogger(__name__)

HandlerType = Literal["NODE_HANDLER", "INFRA_HANDLER", "PROJECTION_HANDLER"]
HandlerCategory = Literal["EFFECT", "COMPUTE", "NONDETERMINISTIC_COMPUTE"]

_GH_PR_FIELDS = (
    "number",
    "title",
    "mergeable",
    "mergeStateStatus",
    "statusCheckRollup",
    "reviewDecision",
    "isDraft",
    "labels",
    "headRefOid",
)

_SUBPROCESS_TIMEOUT_SECONDS = 30

# Shape fields used to determine whether a PR is stalled.
# head_sha is intentionally included: a force-push changes sha → not stalled.
# labels are intentionally excluded: label changes don't indicate progress on blocking state.
_STALL_SHAPE_FIELDS = (
    "mergeable",
    "merge_state_status",
    "review_decision",
    "required_checks_pass",
    "head_sha",
)


def _checks_pass(status_check_rollup: list[dict[str, Any]]) -> bool:
    """Determine if all required status checks pass."""
    if not status_check_rollup:
        return True
    return all(
        item.get("conclusion") == "SUCCESS" or item.get("state") == "SUCCESS"
        for item in status_check_rollup
    )


def _extract_labels(raw_labels: list[dict[str, Any]]) -> list[str]:
    """Extract label name strings from gh JSON label objects."""
    return [lbl.get("name", "") for lbl in raw_labels if lbl.get("name")]


def _parse_pr(raw: dict[str, Any], repo: str) -> ModelPRInfo:
    """Parse a single gh pr list JSON object into a ModelPRInfo."""
    return ModelPRInfo(
        number=raw["number"],
        title=raw.get("title", ""),
        repo=repo,
        mergeable=raw.get("mergeable", "UNKNOWN"),
        merge_state_status=raw.get("mergeStateStatus", "UNKNOWN"),
        is_draft=raw.get("isDraft", False),
        review_decision=raw.get("reviewDecision"),
        required_checks_pass=_checks_pass(raw.get("statusCheckRollup") or []),
        labels=_extract_labels(raw.get("labels") or []),
        head_sha=raw.get("headRefOid") or None,
    )


def _scan_repo(
    repo: str,
    state: str,
    limit: int,
    include_drafts: bool,
) -> ModelRepoScanResult:
    """Scan a single repo via gh pr list subprocess call."""
    fields = ",".join(_GH_PR_FIELDS)
    cmd = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        state,
        "--json",
        fields,
        "--limit",
        str(limit),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "Timeout scanning %s after %ds", repo, _SUBPROCESS_TIMEOUT_SECONDS
        )
        return ModelRepoScanResult(
            repo=repo, error=f"Timeout after {_SUBPROCESS_TIMEOUT_SECONDS}s"
        )
    except Exception as exc:
        logger.warning("Failed to scan %s: %s", repo, exc)
        return ModelRepoScanResult(repo=repo, error=str(exc))

    if result.returncode != 0:
        stderr = result.stderr.strip()
        logger.warning("gh pr list failed for %s: %s", repo, stderr)
        return ModelRepoScanResult(
            repo=repo, error=f"gh exit {result.returncode}: {stderr}"
        )

    try:
        raw_prs: list[dict[str, Any]] = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return ModelRepoScanResult(repo=repo, error=f"JSON parse error: {exc}")

    prs: list[ModelPRInfo] = []
    for raw in raw_prs:
        pr_info = _parse_pr(raw, repo)
        if not include_drafts and pr_info.is_draft:
            continue
        prs.append(pr_info)

    return ModelRepoScanResult(repo=repo, prs=tuple(prs))


def _snapshot_dir() -> Path:
    """Return the pr-snapshots state directory, creating it if needed."""
    omni_home = os.environ.get("OMNI_HOME", str(Path.home() / "Code" / "omni_home"))
    state_dir = Path(omni_home) / ".onex_state" / "pr-snapshots"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _serialize_prs(prs: list[ModelPRInfo]) -> list[dict[str, Any]]:
    """Serialize ModelPRInfo list to JSON-serializable dicts."""
    return [pr.model_dump() for pr in prs]


def _write_snapshot(prs: list[ModelPRInfo], snapshot_dir: Path) -> None:
    """Rotate previous.json ← current.json, write new current.json."""
    current = snapshot_dir / "current.json"
    previous = snapshot_dir / "previous.json"
    if current.exists():
        current.rename(previous)
    current.write_text(
        json.dumps(
            {
                "captured_at": datetime.now(UTC).isoformat(),
                "prs": _serialize_prs(prs),
            },
            default=str,
        )
    )


def _load_previous_snapshot(snapshot_dir: Path) -> list[dict[str, Any]] | None:
    """Load current.json as the baseline before it's rotated to previous.json.

    Must be called before _write_snapshot so we read the last tick's data.
    Returns None on first run (no current.json exists yet).
    """
    current = snapshot_dir / "current.json"
    if not current.exists():
        return None
    try:
        data = json.loads(current.read_text())
        return data.get("prs", [])  # type: ignore[no-any-return]
    except Exception as exc:
        logger.warning("Could not read previous snapshot: %s", exc)
        return None


def _pr_shape_key(pr: dict[str, Any]) -> tuple[Any, ...]:
    """Return the shape tuple used for stall comparison."""
    return tuple(pr.get(f) for f in _STALL_SHAPE_FIELDS)


def _is_pr_blocked(pr: dict[str, Any]) -> bool:
    """Return True if the PR is in a blocking (non-clean) state."""
    mergeable = pr.get("mergeable", "UNKNOWN")
    merge_state = pr.get("merge_state_status", "UNKNOWN")
    return not (mergeable == "MERGEABLE" and merge_state == "CLEAN")


def _blocking_reason(pr: dict[str, Any]) -> str:
    """Build a human-readable blocking reason string from PR shape fields."""
    parts = []
    for field in (
        "mergeable",
        "merge_state_status",
        "review_decision",
        "required_checks_pass",
    ):
        val = pr.get(field)
        if val is not None:
            parts.append(f"{field}={val}")
    return ", ".join(parts) if parts else "unknown"


def _detect_stalls(
    current_prs: list[ModelPRInfo],
    previous_raw: list[dict[str, Any]] | None,
    now: datetime,
) -> tuple[ModelPrStallEvent, ...]:
    """Compare current vs previous snapshot; emit stall events for frozen blocked PRs."""
    if not previous_raw:
        return ()
    prev_index: dict[str, dict[str, Any]] = {}
    for p in previous_raw:
        key = f"{p.get('repo')}#{p.get('number')}"
        prev_index[key] = p

    stall_events: list[ModelPrStallEvent] = []

    for pr in current_prs:
        key = f"{pr.repo}#{pr.number}"
        prev = prev_index.get(key)
        if prev is None:
            continue

        current_dict = pr.model_dump()
        if not _is_pr_blocked(current_dict):
            continue

        if _pr_shape_key(current_dict) == _pr_shape_key(prev):
            stall_events.append(
                ModelPrStallEvent(
                    pr_number=pr.number,
                    repo=pr.repo,
                    stall_count=2,
                    blocking_reason=_blocking_reason(current_dict),
                    first_seen_at=now,
                    last_seen_at=now,
                    head_sha=pr.head_sha,
                )
            )
            logger.warning(
                "Stall detected: %s (reason: %s)", key, _blocking_reason(current_dict)
            )

    return tuple(stall_events)


class HandlerPrSnapshot:
    """Scans GitHub repos for open PRs and produces ModelPRInfo objects.

    Effect node that shells out to ``gh pr list`` per repo. Per-repo error
    isolation ensures partial failures do not block the full scan. The
    ``all_prs`` property on the result returns ``list[ModelPRInfo]`` for
    direct wiring into ``ModelMergeSweepRequest(prs=...)``.

    On each invocation, persists the snapshot to disk and compares against
    the previous snapshot to detect stalled PRs (``stall_events`` on result).
    """

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "EFFECT"

    def handle(self, input_model: ModelPrSnapshotInput) -> ModelPrSnapshotResult:
        """Scan all repos and return aggregate PR snapshot.

        Args:
            input_model: Scan configuration (repos, state, limits).

        Returns:
            ModelPrSnapshotResult with per-repo results, all_prs accessor,
            and stall_events for any PRs frozen across consecutive snapshots.
        """
        logger.info(
            "PR snapshot scanning %d repos (state=%s, limit=%d)",
            len(input_model.repos),
            input_model.state,
            input_model.limit_per_repo,
        )

        repo_results: list[ModelRepoScanResult] = []
        for repo in input_model.repos:
            scan_result = _scan_repo(
                repo=repo,
                state=input_model.state,
                limit=input_model.limit_per_repo,
                include_drafts=input_model.include_drafts,
            )
            repo_results.append(scan_result)
            if scan_result.success:
                logger.info("  %s: %d PRs", repo, len(scan_result.prs))
            else:
                logger.warning("  %s: FAILED — %s", repo, scan_result.error)

        all_prs = [pr for r in repo_results for pr in r.prs]

        snapshot_dir = _snapshot_dir()
        previous_raw = _load_previous_snapshot(snapshot_dir)
        _write_snapshot(all_prs, snapshot_dir)

        now = datetime.now(UTC)
        stall_events: tuple[ModelPrStallEvent, ...] = ()
        if previous_raw is not None:
            stall_events = _detect_stalls(all_prs, previous_raw, now)

        result = ModelPrSnapshotResult(
            repo_results=tuple(repo_results),
            stall_events=stall_events,
        )
        logger.info(
            "PR snapshot complete: %d total PRs, %d failed repos, %d stalls",
            result.total_prs,
            len(result.failed_repos),
            len(result.stall_events),
        )
        return result


__all__: list[str] = ["HandlerPrSnapshot"]
