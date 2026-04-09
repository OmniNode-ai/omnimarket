# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GitHub PR probe — captures open PRs across OmniNode repos via gh CLI."""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    ModelGitHubPRSnapshot,
)

logger = logging.getLogger(__name__)

_OMNINODE_REPOS = [
    "OmniNode-ai/omnimarket",
    "OmniNode-ai/omniclaude",
    "OmniNode-ai/omnibase_core",
    "OmniNode-ai/omnibase_infra",
    "OmniNode-ai/omnibase_spi",
    "OmniNode-ai/omnidash",
    "OmniNode-ai/omniintelligence",
    "OmniNode-ai/omnimemory",
    "OmniNode-ai/omninode_infra",
    "OmniNode-ai/omniweb",
    "OmniNode-ai/onex_change_control",
    "OmniNode-ai/omnibase_compat",
    "OmniNode-ai/omnigemini",
]

_GH_JSON_FIELDS = "number,title,repository,state,labels,createdAt,statusCheckRollup"


class ProbeGitHubPRs:
    """Capture open GitHub PRs across OmniNode repositories."""

    name: str = "github_prs"

    async def collect(self) -> list[ModelGitHubPRSnapshot]:
        results: list[ModelGitHubPRSnapshot] = []
        now = datetime.now(UTC)
        for repo in _OMNINODE_REPOS:
            try:
                proc = subprocess.run(
                    [
                        "gh",
                        "pr",
                        "list",
                        "--repo",
                        repo,
                        "--state",
                        "open",
                        "--json",
                        _GH_JSON_FIELDS,
                        "--limit",
                        "200",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if proc.returncode != 0:
                    logger.warning(
                        "gh pr list failed for %s: %s", repo, proc.stderr.strip()
                    )
                    continue
                prs: list[dict[str, object]] = json.loads(proc.stdout)
                for pr in prs:
                    created_str = str(pr.get("createdAt", ""))
                    try:
                        created_at = datetime.fromisoformat(
                            created_str.replace("Z", "+00:00")
                        )
                        age_days = (now - created_at).total_seconds() / 86400.0
                    except (ValueError, TypeError):
                        age_days = 0.0

                    raw_labels = pr.get("labels", [])
                    labels: list[str] = []
                    if isinstance(raw_labels, list):
                        for lbl in raw_labels:
                            if isinstance(lbl, dict):
                                lbl_name = lbl.get("name", "")
                                labels.append(str(lbl_name))

                    # ci_status from statusCheckRollup
                    rollup = pr.get("statusCheckRollup")
                    ci_status: str | None = None
                    if isinstance(rollup, list) and rollup:
                        states = {
                            str(r.get("state") or r.get("conclusion") or "")
                            for r in rollup
                            if isinstance(r, dict)
                        }
                        if "FAILURE" in states:
                            ci_status = "failure"
                        elif "PENDING" in states or "IN_PROGRESS" in states:
                            ci_status = "pending"
                        elif states - {""}:
                            ci_status = "success"
                    elif isinstance(rollup, str):
                        ci_status = rollup.lower()

                    repo_obj = pr.get("repository")
                    repo_name = (
                        str(repo_obj.get("nameWithOwner", repo))
                        if isinstance(repo_obj, dict)
                        else repo
                    )

                    results.append(
                        ModelGitHubPRSnapshot(
                            pr_number=pr["number"],
                            title=pr.get("title", ""),
                            repo=repo_name,
                            state=pr.get("state", "open"),
                            labels=labels,
                            age_days=round(age_days, 2),
                            ci_status=ci_status,
                        )
                    )
            except Exception:
                logger.warning("Failed to collect PRs for repo %s", repo, exc_info=True)
                continue
        return results
