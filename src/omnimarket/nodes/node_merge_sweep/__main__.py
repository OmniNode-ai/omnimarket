# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_merge_sweep.

Reads open PRs from GitHub (via gh CLI), classifies them into tracks, and
outputs a JSON classification report to stdout.

Usage:
    python -m omnimarket.nodes.node_merge_sweep \
        --repos OmniNode-ai/omniclaude,OmniNode-ai/omnibase_core \
        --require-approval \
        --merge-method squash \
        --dry-run

Outputs JSON to stdout: ModelMergeSweepResult model.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import subprocess
import sys

from omnimarket.nodes.node_merge_sweep.handlers.handler_merge_sweep import (
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

_PR_FIELDS = (
    "number,title,mergeable,mergeStateStatus,statusCheckRollup,"
    "reviewDecision,headRefName,baseRefName,isDraft,labels,author,updatedAt"
)


def _fetch_prs(repo: str) -> list[dict]:  # type: ignore[type-arg]
    """Fetch open PRs for a repo via gh CLI."""
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--json",
            _PR_FIELDS,
            "--limit",
            "100",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _log.warning("gh pr list failed for %s: %s", repo, result.stderr.strip())
        return []
    try:
        return json.loads(result.stdout)  # type: ignore[no-any-return]
    except json.JSONDecodeError as exc:
        _log.warning("failed to parse gh output for %s: %s", repo, exc)
        return []


def _is_green(pr: dict) -> bool:  # type: ignore[type-arg]
    """All required checks pass."""
    rollup = pr.get("statusCheckRollup") or []
    required = [c for c in rollup if c.get("isRequired")]
    if not required:
        return True
    return all(c.get("conclusion") == "SUCCESS" for c in required)


def _to_pr_info(pr: dict, repo: str) -> ModelPRInfo:  # type: ignore[type-arg]
    return ModelPRInfo(
        number=pr["number"],
        title=pr.get("title", ""),
        repo=repo,
        mergeable=pr.get("mergeable", "UNKNOWN"),
        merge_state_status=pr.get("mergeStateStatus", "UNKNOWN"),
        is_draft=pr.get("isDraft", False),
        review_decision=pr.get("reviewDecision"),
        required_checks_pass=_is_green(pr),
        labels=[lbl["name"] for lbl in (pr.get("labels") or [])],
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

    args = parser.parse_args()

    repos = [r.strip() for r in args.repos.split(",") if r.strip()] or _DEFAULT_REPOS

    all_prs: list[ModelPRInfo] = []
    for repo in repos:
        for pr in _fetch_prs(repo):
            all_prs.append(_to_pr_info(pr, repo))

    failure_history = _load_failure_history(onex_state_dir)

    request = ModelMergeSweepRequest(
        prs=all_prs,
        require_approval=args.require_approval,
        merge_method=args.merge_method,
        max_total_merges=args.max_total_merges,
        skip_polish=args.skip_polish,
        failure_history=failure_history,
    )

    handler = NodeMergeSweep()
    result = handler.handle(request)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if result.status in ("error",):
        sys.exit(1)


if __name__ == "__main__":
    main()
