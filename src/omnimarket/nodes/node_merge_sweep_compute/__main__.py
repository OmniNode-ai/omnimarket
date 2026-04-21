# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_merge_sweep.

Reads open PRs from GitHub (via HTTP adapter), classifies them into tracks,
and outputs a JSON classification report to stdout.

Usage:
    python -m omnimarket.nodes.node_merge_sweep \
        --repos OmniNode-ai/omniclaude,OmniNode-ai/omnibase_core \
        --require-approval \
        --merge-method squash \
        --dry-run

Outputs JSON to stdout: ModelMergeSweepResult model.

Environment:
    GH_PAT   GitHub PAT (required — fail-fast if missing)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys
from typing import Any

from omnimarket.nodes.node_merge_sweep_compute.adapter_github_http import (
    GitHubHttpClient,
)
from omnimarket.nodes.node_merge_sweep_compute.branch_protection import (
    BranchProtectionCache,
)
from omnimarket.nodes.node_merge_sweep_compute.handlers.handler_merge_sweep import (
    ModelFailureHistoryEntry,
    ModelMergeSweepRequest,
    ModelPRInfo,
    NodeMergeSweep,
)

_log = logging.getLogger(__name__)

_DEFAULT_REPOS = [
    "OmniNode-ai/omniclaude",
    "OmniNode-ai/omnibase_core",
    "OmniNode-ai/omnibase_infra",
    "OmniNode-ai/omnibase_spi",
    "OmniNode-ai/omniintelligence",
    "OmniNode-ai/omnimemory",
    "OmniNode-ai/omninode_infra",
    "OmniNode-ai/omnidash",
    "OmniNode-ai/onex_change_control",
    "OmniNode-ai/omnimarket",
    "OmniNode-ai/omnibase_compat",
    "OmniNode-ai/omniweb",
]


def _is_green(pr: dict[str, Any]) -> bool:
    rollup = pr.get("statusCheckRollup") or []
    required = [c for c in rollup if isinstance(c, dict) and c.get("isRequired")]
    if not required:
        return True
    return all(
        isinstance(c, dict) and c.get("conclusion") == "SUCCESS" for c in required
    )


def _to_pr_info(
    pr: dict[str, Any], repo: str, required_approving: int | None
) -> ModelPRInfo:
    review_decision_raw = pr.get("reviewDecision")
    review_decision = review_decision_raw if review_decision_raw else None
    return ModelPRInfo(
        number=pr["number"],
        title=pr.get("title", ""),
        repo=repo,
        mergeable=pr.get("mergeable", "UNKNOWN"),
        merge_state_status=pr.get("mergeStateStatus", "UNKNOWN"),
        is_draft=pr.get("isDraft", False),
        review_decision=review_decision,
        required_checks_pass=_is_green(pr),
        labels=[
            lbl["name"] for lbl in (pr.get("labels") or []) if isinstance(lbl, dict)
        ],
        required_approving_review_count=required_approving,
    )


def _load_failure_history(state_dir: str) -> dict[str, ModelFailureHistoryEntry]:
    history_path = pathlib.Path(state_dir) / "merge-sweep" / "failure-history.json"
    if not history_path.exists():
        return {}
    try:
        raw = json.loads(history_path.read_text())
        return {
            k: ModelFailureHistoryEntry(**v)
            for k, v in raw.items()
            if isinstance(v, dict)
        }
    except Exception as exc:
        _log.warning("failed to load failure history: %s", exc)
        return {}


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    onex_state_dir = os.environ.get(
        "ONEX_STATE_DIR", os.path.expanduser("~/.onex_state")
    )

    parser = argparse.ArgumentParser(
        description="Classify open PRs into merge tracks (A-update, A, A-resolve, B, skip)."
    )
    parser.add_argument(
        "--repos",
        default="",
        help="Comma-separated org/repo names (default: all OmniNode repos)",
    )
    parser.add_argument(
        "--require-approval",
        action="store_true",
        default=True,
        help="Require GitHub review approval (default: true)",
    )
    parser.add_argument(
        "--no-require-approval", dest="require_approval", action="store_false"
    )
    parser.add_argument(
        "--merge-method",
        default="squash",
        choices=["squash", "merge", "rebase"],
        help="Merge strategy (default: squash)",
    )
    parser.add_argument(
        "--max-total-merges",
        type=int,
        default=0,
        help="Hard cap on Track A candidates (0 = unlimited)",
    )
    parser.add_argument("--skip-polish", action="store_true", default=False)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print classification without performing any merges",
    )
    parser.add_argument(
        "--use-lifecycle-ordering",
        action="store_true",
        default=False,
        help=(
            "Reorder Track A PRs via the lifecycle triage→reducer pipeline "
            "for dependency-optimal merge order (default: flat listing order)"
        ),
    )

    args = parser.parse_args()
    repos = [r.strip() for r in args.repos.split(",") if r.strip()] or _DEFAULT_REPOS

    # Fail-fast: GH_PAT must be present
    github = GitHubHttpClient()

    all_prs: list[ModelPRInfo] = []
    protection = BranchProtectionCache(github)
    for repo in repos:
        required_approving = protection.required_approving_review_count(repo)
        for pr in github.fetch_open_prs(repo):
            all_prs.append(_to_pr_info(pr, repo, required_approving))

    failure_history = _load_failure_history(onex_state_dir)

    request = ModelMergeSweepRequest(
        prs=all_prs,
        require_approval=args.require_approval,
        merge_method=args.merge_method,
        max_total_merges=args.max_total_merges,
        skip_polish=args.skip_polish,
        failure_history=failure_history,
        use_lifecycle_ordering=args.use_lifecycle_ordering,
    )

    handler = NodeMergeSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status in ("error",):
        sys.exit(1)


if __name__ == "__main__":
    main()
