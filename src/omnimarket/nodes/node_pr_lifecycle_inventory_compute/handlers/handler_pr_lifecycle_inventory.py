# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for pr_lifecycle_inventory_compute node.

Collects raw PR state from GitHub via gh CLI.
Pure data collection — no classification or action logic.

Related:
    - OMN-8082: Create pr_lifecycle_inventory_compute Node
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime
from typing import Literal

from omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory import (
    ModelPrCheckRun,
    ModelPrInventoryInput,
    ModelPrInventoryOutput,
    ModelPrReview,
    ModelPrState,
    ModelStuckQueueEntry,
)

_STUCK_QUEUE_THRESHOLD_MINUTES = 30
_QUEUE_STALL_THRESHOLD_MINUTES = 15

logger = logging.getLogger(__name__)

HandlerType = Literal["NODE_HANDLER"]
HandlerCategory = Literal["COMPUTE"]

HANDLER_TYPE: HandlerType = "NODE_HANDLER"
HANDLER_CATEGORY: HandlerCategory = "COMPUTE"


class HandlerPrLifecycleInventory:
    """Collects raw PR state from GitHub via gh CLI.

    Inventory layer of the pr_lifecycle domain — collects raw PR data
    without making any classification or action decisions.
    """

    @property
    def handler_type(self) -> HandlerType:
        return HANDLER_TYPE

    @property
    def handler_category(self) -> HandlerCategory:
        return HANDLER_CATEGORY

    def handle(self, input_model: ModelPrInventoryInput) -> ModelPrInventoryOutput:
        """Collect raw PR state for all requested PR numbers.

        Args:
            input_model: Repo and list of PR numbers to collect.

        Returns:
            ModelPrInventoryOutput with collected PR states.
        """
        pr_states: list[ModelPrState] = []
        errors: list[str] = []

        for pr_number in input_model.pr_numbers:
            try:
                state = self._collect_pr_state(input_model.repo, pr_number)
                pr_states.append(state)
            except Exception as exc:
                msg = f"PR #{pr_number}: {exc}"
                logger.warning("Failed to collect PR state: %s", msg)
                errors.append(msg)

        stuck = self._detect_stuck_queue_prs(input_model.repo, pr_states)

        return ModelPrInventoryOutput(
            repo=input_model.repo,
            pr_states=tuple(pr_states),
            total_collected=len(pr_states),
            collection_errors=tuple(errors),
            stuck_queue_prs=stuck,
        )

    def _detect_stuck_queue_prs(
        self, repo: str, pr_states: list[ModelPrState]
    ) -> list[ModelStuckQueueEntry]:
        """Detect PRs that have been stuck in the merge queue past a threshold.

        Legacy stuck queue entries are PRs queued for more than 30 minutes.
        Merge-group check-suite stalls are PRs with a mergeQueueEntry in
        AWAITING_CHECKS for more than 15 minutes and zero merge_group runs for
        the queue head SHA. Wraps gh API calls in try/except so repos without
        merge queue support silently return an empty list.

        Args:
            repo: GitHub repo slug.
            pr_states: Collected PR states to check.

        Returns:
            List of stuck queue entries (may be empty).
        """
        candidate_prs = [p for p in pr_states if p.state == "open"]
        if not candidate_prs:
            return []

        stuck: list[ModelStuckQueueEntry] = []
        now = datetime.now(tz=UTC)

        for pr in candidate_prs:
            try:
                result = subprocess.run(
                    [
                        "gh",
                        "pr",
                        "view",
                        str(pr.pr_number),
                        "--repo",
                        repo,
                        "--json",
                        "mergeQueueEntry",
                    ],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    continue
                data: dict[str, object] = json.loads(result.stdout)
                entry = data.get("mergeQueueEntry")
                if not entry or not isinstance(entry, dict):
                    # Repo plan doesn't support merge queues — skip silently
                    continue
                enqueued_at_raw = entry.get("enqueuedAt")
                if not enqueued_at_raw:
                    continue
                queue_state = str(entry.get("state") or "QUEUED")
                enqueued_at = datetime.fromisoformat(
                    str(enqueued_at_raw).replace("Z", "+00:00")
                )
                age_minutes = (now - enqueued_at).total_seconds() / 60

                if queue_state == "AWAITING_CHECKS":
                    head_sha = self._extract_merge_queue_head_sha(entry)
                    if not head_sha or age_minutes <= _QUEUE_STALL_THRESHOLD_MINUTES:
                        continue
                    merge_group_run_count = self._count_merge_group_runs(repo, head_sha)
                    if merge_group_run_count != 0:
                        continue
                    stuck.append(
                        ModelStuckQueueEntry(
                            pr_number=pr.pr_number,
                            repo=repo,
                            title=pr.title,
                            queue_entered_at=enqueued_at,
                            queue_age_minutes=age_minutes,
                            queue_state=queue_state,
                            head_sha=head_sha,
                            merge_group_run_count=merge_group_run_count,
                        )
                    )
                    logger.warning(
                        "Merge queue check-suite dispatch stall detected: "
                        "%s#%s head_sha=%s age=%.1f min merge_group_runs=0",
                        repo,
                        pr.pr_number,
                        head_sha,
                        age_minutes,
                    )
                    continue

                if (
                    pr.merge_state_status == "QUEUED"
                    and age_minutes > _STUCK_QUEUE_THRESHOLD_MINUTES
                ):
                    stuck.append(
                        ModelStuckQueueEntry(
                            pr_number=pr.pr_number,
                            repo=repo,
                            title=pr.title,
                            queue_entered_at=enqueued_at,
                            queue_age_minutes=age_minutes,
                            queue_state=queue_state,
                        )
                    )
                    logger.warning(
                        "Stuck merge queue PR detected: %s#%s age=%.1f min",
                        repo,
                        pr.pr_number,
                        age_minutes,
                    )
            except Exception as exc:
                logger.debug(
                    "Could not check merge queue entry for %s#%s: %s",
                    repo,
                    pr.pr_number,
                    exc,
                )

        return stuck

    @staticmethod
    def _extract_merge_queue_head_sha(entry: dict[str, object]) -> str | None:
        """Extract the queue head SHA from a gh mergeQueueEntry payload."""
        for key in ("headSha", "headSHA", "headCommitOid", "headCommitOID"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                return value

        head_commit = entry.get("headCommit")
        if isinstance(head_commit, dict):
            for key in ("oid", "id"):
                value = head_commit.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    def _count_merge_group_runs(self, repo: str, head_sha: str) -> int | None:
        """Count merge_group workflow runs for the queue head SHA."""
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repo}/actions/runs?head_sha={head_sha}&event=merge_group",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.debug(
                "Could not count merge_group runs for %s head_sha=%s: %s",
                repo,
                head_sha,
                result.stderr.strip(),
            )
            return None
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            logger.debug(
                "merge_group run count returned invalid JSON for %s head_sha=%s",
                repo,
                head_sha,
            )
            return None
        total_count = payload.get("total_count", 0)
        return total_count if isinstance(total_count, int) else None

    def _collect_pr_state(self, repo: str, pr_number: int) -> ModelPrState:
        """Collect state for a single PR via gh CLI.

        Args:
            repo: GitHub repo slug.
            pr_number: PR number.

        Returns:
            ModelPrState with collected data.

        Raises:
            RuntimeError: If gh CLI call fails.
        """
        pr_data = self._gh_pr_view(repo, pr_number)
        check_runs = self._collect_check_runs(repo, pr_number)
        reviews = self._collect_reviews(repo, pr_number)

        state_raw = str(pr_data.get("state", "open")).lower()
        if state_raw == "merged":
            state: Literal["open", "closed", "merged"] = "merged"
        elif state_raw == "closed":
            state = "closed"
        else:
            state = "open"

        mergeable = pr_data.get("mergeable") or None
        merge_state_status = pr_data.get("mergeStateStatus") or None
        review_decision = pr_data.get("reviewDecision") or None

        has_conflicts = mergeable == "CONFLICTING" or (
            merge_state_status is not None and merge_state_status == "DIRTY"
        )

        # CI passing: True if all completed checks succeeded, False if any failed
        ci_passing: bool | None = None
        completed = [
            c
            for c in check_runs
            if c.status == "completed" and c.conclusion is not None
        ]
        if completed:
            ci_passing = all(
                c.conclusion in ("success", "skipped", "neutral") for c in completed
            )

        base_ref_data = pr_data.get("baseRefName") or pr_data.get("base", {})
        head_ref_data = pr_data.get("headRefName") or pr_data.get("head", {})

        return ModelPrState(
            repo=repo,
            pr_number=pr_number,
            title=pr_data.get("title", ""),
            state=state,
            is_draft=pr_data.get("isDraft", False),
            mergeable=mergeable,
            merge_state_status=merge_state_status,
            review_decision=review_decision,
            head_ref=head_ref_data if isinstance(head_ref_data, str) else "",
            base_ref=base_ref_data if isinstance(base_ref_data, str) else "",
            check_runs=tuple(check_runs),
            reviews=tuple(reviews),
            has_conflicts=has_conflicts,
            ci_passing=ci_passing,
        )

    def _gh_pr_view(self, repo: str, pr_number: int) -> dict[str, object]:
        """Run gh pr view and return parsed JSON.

        Args:
            repo: GitHub repo slug.
            pr_number: PR number.

        Returns:
            Parsed JSON dict from gh output.

        Raises:
            RuntimeError: If the gh command fails.
        """
        cmd = [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "title,state,isDraft,mergeable,mergeStateStatus,reviewDecision,"
            "baseRefName,headRefName",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"gh pr view failed (exit {result.returncode}): {result.stderr.strip()}"
            )
        return json.loads(result.stdout)  # type: ignore[no-any-return]

    def _collect_check_runs(self, repo: str, pr_number: int) -> list[ModelPrCheckRun]:
        """Collect CI check runs for a PR via gh pr checks.

        Returns empty list on failure (non-fatal).
        """
        cmd = [
            "gh",
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "name,state,conclusion",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.debug(
                "gh pr checks failed for PR #%d in %s: %s",
                pr_number,
                repo,
                result.stderr.strip(),
            )
            return []
        try:
            raw: list[dict[str, object]] = json.loads(result.stdout)
            return [
                ModelPrCheckRun(
                    name=str(item.get("name", "")),
                    status=str(item.get("state", "unknown")),
                    conclusion=str(item["conclusion"])
                    if item.get("conclusion")
                    else None,
                )
                for item in raw
            ]
        except (json.JSONDecodeError, KeyError) as exc:
            logger.debug("Failed to parse check runs for PR #%d: %s", pr_number, exc)
            return []

    @staticmethod
    def _extract_review_author(review: dict[str, object]) -> str:
        """Extract author login from a review dict (handles str or nested author object)."""
        author = review.get("author")
        if isinstance(author, str):
            return author
        if isinstance(author, dict):
            return str(author.get("login", ""))
        return ""

    def _collect_reviews(self, repo: str, pr_number: int) -> list[ModelPrReview]:
        """Collect PR reviews via gh pr view --json reviews.

        Returns empty list on failure (non-fatal).
        """
        cmd = [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "reviews",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return []
        try:
            data: dict[str, list[dict[str, object]]] = json.loads(result.stdout)
            raw_reviews = data.get("reviews", [])
            return [
                ModelPrReview(
                    author=self._extract_review_author(review),
                    state=str(review.get("state", "")),
                )
                for review in raw_reviews
            ]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.debug("Failed to parse reviews for PR #%d: %s", pr_number, exc)
            return []
