"""GitHub PR probe — captures open PRs across all OmniNode repos."""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    ModelGitHubPRSnapshot,
    ProbeSnapshotItem,
)

logger = logging.getLogger(__name__)

_OMNINODE_REPOS = [
    "OmniNode-ai/omniclaude",
    "OmniNode-ai/omnibase_core",
    "OmniNode-ai/omnibase_infra",
    "OmniNode-ai/omnibase_spi",
    "OmniNode-ai/omnidash",
    "OmniNode-ai/omniintelligence",
    "OmniNode-ai/omnimemory",
    "OmniNode-ai/omnimarket",
    "OmniNode-ai/omninode_infra",
    "OmniNode-ai/omniweb",
    "OmniNode-ai/onex_change_control",
]


class ProbeGitHubPRs:
    """Probe that collects open GitHub PRs across all OmniNode repositories."""

    name: str = "github_prs"

    async def collect(self, omni_home: str) -> list[ProbeSnapshotItem]:
        """Collect open PRs using gh CLI.

        Returns an empty list on any failure — probe errors are non-fatal.
        """
        results: list[ProbeSnapshotItem] = []
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
                        "number,title,labels,createdAt,statusCheckRollup",
                        "--limit",
                        "200",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if proc.returncode != 0:
                    logger.warning(
                        "gh pr list failed for %s: %s", repo, proc.stderr[:200]
                    )
                    continue

                prs = json.loads(proc.stdout)
                for pr in prs:
                    created_at_str = pr.get("createdAt", "")
                    try:
                        created_at = datetime.fromisoformat(
                            created_at_str.replace("Z", "+00:00")
                        )
                        age_days = (now - created_at).total_seconds() / 86400
                    except (ValueError, AttributeError):
                        age_days = 0.0

                    labels = [lbl.get("name", "") for lbl in pr.get("labels", [])]

                    # Determine CI status from statusCheckRollup
                    checks = pr.get("statusCheckRollup") or []
                    if not checks:
                        ci_status = None
                    elif all(c.get("conclusion") == "SUCCESS" for c in checks):
                        ci_status = "success"
                    elif any(c.get("conclusion") == "FAILURE" for c in checks):
                        ci_status = "failure"
                    else:
                        ci_status = "pending"

                    results.append(
                        ModelGitHubPRSnapshot(
                            pr_number=pr["number"],
                            title=pr.get("title", ""),
                            repo=repo,
                            state="open",
                            labels=labels,
                            age_days=round(age_days, 2),
                            ci_status=ci_status,
                        )
                    )
            except Exception as exc:
                logger.warning("Failed to collect PRs for %s: %s", repo, exc)
                continue

        return results


__all__: list[str] = ["ProbeGitHubPRs"]
