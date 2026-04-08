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
import subprocess
from typing import Any, Literal

from omnimarket.nodes.node_merge_sweep.handlers.handler_merge_sweep import (
    ModelPRInfo,
)
from omnimarket.nodes.node_pr_snapshot_effect.models.model_pr_snapshot_input import (
    ModelPrSnapshotInput,
)
from omnimarket.nodes.node_pr_snapshot_effect.models.model_pr_snapshot_result import (
    ModelPrSnapshotResult,
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
)

_SUBPROCESS_TIMEOUT_SECONDS = 30


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


class HandlerPrSnapshot:
    """Scans GitHub repos for open PRs and produces ModelPRInfo objects.

    Effect node that shells out to ``gh pr list`` per repo. Per-repo error
    isolation ensures partial failures do not block the full scan. The
    ``all_prs`` property on the result returns ``list[ModelPRInfo]`` for
    direct wiring into ``ModelMergeSweepRequest(prs=...)``.
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
            ModelPrSnapshotResult with per-repo results and all_prs accessor.
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

        result = ModelPrSnapshotResult(repo_results=tuple(repo_results))
        logger.info(
            "PR snapshot complete: %d total PRs, %d failed repos",
            result.total_prs,
            len(result.failed_repos),
        )
        return result


__all__: list[str] = ["HandlerPrSnapshot"]
