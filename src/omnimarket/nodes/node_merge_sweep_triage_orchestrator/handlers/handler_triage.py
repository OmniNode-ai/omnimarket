# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_merge_sweep_triage_orchestrator [OMN-8959].

ORCHESTRATOR node. Consumes ModelMergeSweepResult (classified PRs), fans out
N typed command events across 3 mechanical effect topics per the 14-row
classification-to-action decision table.

Decision table (evaluated in order; first match wins):
 1. is_draft=True         → SKIP (any track)
 2. A_UPDATE, MERGEABLE, CLEAN, APPROVED, checks_pass → ModelAutoMergeArmCommand
 3. A_UPDATE, MERGEABLE, BEHIND, APPROVED, checks_pass → ModelRebaseCommand
 4. A_UPDATE, MERGEABLE, BEHIND, not APPROVED           → SKIP (needs human review)
 5. A_RESOLVE (any)                                     → SKIP (Phase 2 LLM)
 6. B_POLISH, MERGEABLE, BLOCKED, checks fail           → ModelCiRerunCommand
 7. B_POLISH, CONFLICTING, DIRTY                        → SKIP (Phase 2 LLM)
 8. B_POLISH, MERGEABLE, BEHIND, checks fail            → ModelRebaseCommand
 9. B_POLISH, MERGEABLE, DIRTY                          → SKIP (Phase 2 LLM)
10. SKIP track                                          → SKIP
11. UNKNOWN mergeable                                   → SKIP + WARN
12. UNKNOWN merge_state_status                          → SKIP + WARN
13. CHANGES_REQUESTED review decision                   → SKIP
14. (fallthrough)                                       → SKIP + WARN
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_merge_sweep.handlers.handler_merge_sweep import (
    EnumPRTrack,
    ModelClassifiedPR,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelAutoMergeArmCommand,
    ModelCiRerunCommand,
    ModelRebaseCommand,
    ModelTriageRequest,
)

_log = logging.getLogger(__name__)

# Topics from contract.yaml — never inline elsewhere
TOPIC_AUTO_MERGE_ARM = "onex.cmd.omnimarket.pr-auto-merge-arm.v1"
TOPIC_REBASE = "onex.cmd.omnimarket.pr-rebase.v1"
TOPIC_CI_RERUN = "onex.cmd.omnimarket.pr-ci-rerun.v1"

_PROTECTED_BASES = {"main", "master", "develop"}


class HandlerTriageOrchestrator:
    """ORCHESTRATOR — fans out N typed command events per 14-row decision table.

    GraphQL node ID resolution happens inline via subprocess. If resolution fails,
    the orchestrator skips the PR (logs failure, does NOT emit a command for it).
    Resolution failures are tracked in run_metadata but do NOT count toward total_prs
    (only actionable PRs count).
    """

    async def handle(self, request: ModelTriageRequest) -> ModelHandlerOutput:  # type: ignore[type-arg]
        """Classify each PR and emit the appropriate command event."""
        raw_cmds: list[Any] = []

        # First pass: collect actionable commands with placeholder total_prs=0
        for classified_pr in request.classification.classified:
            cmd = await self._classify_to_command(
                classified_pr,
                request.run_id,
                request.correlation_id,
                0,  # placeholder; replaced below
            )
            if cmd is not None:
                raw_cmds.append(cmd)

        # total_prs = actionable count only; skipped PRs never emit outcomes
        total_prs = len(raw_cmds)
        events: list[Any] = [
            cmd.model_copy(update={"total_prs": total_prs}) for cmd in raw_cmds
        ]

        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="node_merge_sweep_triage_orchestrator",
            events=tuple(events),
        )

    async def _classify_to_command(
        self,
        classified: ModelClassifiedPR,
        run_id: Any,
        correlation_id: Any,
        total_prs: int,
    ) -> ModelAutoMergeArmCommand | ModelRebaseCommand | ModelCiRerunCommand | None:
        """Apply 14-row decision table. Returns command or None (SKIP)."""
        pr = classified.pr
        track = classified.track

        # Rule 1: draft PRs are inert — always skip
        if pr.is_draft:
            _log.debug("PR %s/%s: SKIP (is_draft)", pr.repo, pr.number)
            return None

        # Rule 13: CHANGES_REQUESTED — do not mutate
        if pr.review_decision == "CHANGES_REQUESTED":
            _log.debug("PR %s/%s: SKIP (CHANGES_REQUESTED)", pr.repo, pr.number)
            return None

        # Rule 11: unknown mergeable — wait for GitHub to compute
        if pr.mergeable == "UNKNOWN":
            _log.warning(
                "PR %s/%s: SKIP (mergeable=UNKNOWN) — GitHub still computing",
                pr.repo,
                pr.number,
            )
            return None

        # Rule 12: unknown merge_state_status — wait
        if pr.merge_state_status == "UNKNOWN":
            _log.warning(
                "PR %s/%s: SKIP (merge_state_status=UNKNOWN) — GitHub still computing",
                pr.repo,
                pr.number,
            )
            return None

        # Rule 5: A_RESOLVE — Phase 2 LLM slot
        if track == EnumPRTrack.A_RESOLVE:
            _log.debug("PR %s/%s: SKIP (A_RESOLVE — Phase 2 LLM)", pr.repo, pr.number)
            return None

        # Rule 10: explicit SKIP track
        if track == EnumPRTrack.SKIP:
            _log.debug("PR %s/%s: SKIP (SKIP track)", pr.repo, pr.number)
            return None

        # Track A_UPDATE rules
        if track == EnumPRTrack.A_UPDATE:
            # Rule 2: CLEAN + APPROVED + checks passing → arm auto-merge
            if (
                pr.mergeable == "MERGEABLE"
                and pr.merge_state_status == "CLEAN"
                and pr.review_decision == "APPROVED"
                and pr.required_checks_pass
            ):
                pr_node_id, head_ref_name = await self._resolve_pr_graphql_id(
                    pr.repo, pr.number
                )
                if pr_node_id is None:
                    _log.error(
                        "PR %s/%s: SKIP — failed to resolve GraphQL node ID",
                        pr.repo,
                        pr.number,
                    )
                    return None
                return ModelAutoMergeArmCommand(
                    pr_number=pr.number,
                    repo=pr.repo,
                    pr_node_id=pr_node_id,
                    head_ref_name=head_ref_name or "",
                    correlation_id=correlation_id,
                    run_id=run_id,
                    total_prs=total_prs,
                )

            # Rule 3: BEHIND + APPROVED + checks passing → rebase
            if (
                pr.mergeable == "MERGEABLE"
                and pr.merge_state_status == "BEHIND"
                and pr.review_decision == "APPROVED"
                and pr.required_checks_pass
            ):
                refs = await self._resolve_pr_refs(pr.repo, pr.number)
                if refs is None:
                    _log.error(
                        "PR %s/%s: SKIP — failed to resolve PR refs for rebase",
                        pr.repo,
                        pr.number,
                    )
                    return None
                head_ref, base_ref, head_oid = refs
                return ModelRebaseCommand(
                    pr_number=pr.number,
                    repo=pr.repo,
                    head_ref_name=head_ref,
                    base_ref_name=base_ref,
                    head_ref_oid=head_oid,
                    correlation_id=correlation_id,
                    run_id=run_id,
                    total_prs=total_prs,
                )

            # Rule 4: BEHIND but not APPROVED — needs human review
            if pr.mergeable == "MERGEABLE" and pr.merge_state_status == "BEHIND":
                _log.debug(
                    "PR %s/%s: SKIP A_UPDATE BEHIND — needs human review before mutation",
                    pr.repo,
                    pr.number,
                )
                return None

        # Track B_POLISH rules
        if track == EnumPRTrack.B_POLISH:
            # Rule 7: CONFLICTING + DIRTY → Phase 2 LLM
            if pr.mergeable == "CONFLICTING" and pr.merge_state_status == "DIRTY":
                _log.debug(
                    "PR %s/%s: SKIP B_POLISH CONFLICTING/DIRTY — Phase 2 LLM conflict-hunk",
                    pr.repo,
                    pr.number,
                )
                return None

            # Rule 9: DIRTY (not CONFLICTING) → Phase 2 LLM
            if pr.merge_state_status == "DIRTY":
                _log.debug(
                    "PR %s/%s: SKIP B_POLISH DIRTY — Phase 2 LLM",
                    pr.repo,
                    pr.number,
                )
                return None

            # Rule 6: MERGEABLE + BLOCKED + checks failing → CI rerun
            if (
                pr.mergeable == "MERGEABLE"
                and pr.merge_state_status == "BLOCKED"
                and not pr.required_checks_pass
            ):
                run_id_github = await self._resolve_failing_run_id(pr.repo, pr.number)
                if run_id_github is None:
                    _log.warning(
                        "PR %s/%s: SKIP B_POLISH BLOCKED — no failing run found",
                        pr.repo,
                        pr.number,
                    )
                    return None
                return ModelCiRerunCommand(
                    pr_number=pr.number,
                    repo=pr.repo,
                    run_id_github=run_id_github,
                    correlation_id=correlation_id,
                    run_id=run_id,
                    total_prs=total_prs,
                )

            # Rule 8: MERGEABLE + BEHIND + checks failing → rebase first
            if (
                pr.mergeable == "MERGEABLE"
                and pr.merge_state_status == "BEHIND"
                and not pr.required_checks_pass
            ):
                refs = await self._resolve_pr_refs(pr.repo, pr.number)
                if refs is None:
                    _log.error(
                        "PR %s/%s: SKIP B_POLISH BEHIND — failed to resolve PR refs",
                        pr.repo,
                        pr.number,
                    )
                    return None
                head_ref, base_ref, head_oid = refs
                return ModelRebaseCommand(
                    pr_number=pr.number,
                    repo=pr.repo,
                    head_ref_name=head_ref,
                    base_ref_name=base_ref,
                    head_ref_oid=head_oid,
                    correlation_id=correlation_id,
                    run_id=run_id,
                    total_prs=total_prs,
                )

        # Rule 14: fallthrough — unclassified combination
        _log.warning(
            "PR %s/%s track=%s: SKIP (fallthrough — unclassified combination)",
            pr.repo,
            pr.number,
            track,
        )
        return None

    async def _resolve_pr_graphql_id(
        self, repo: str, pr_number: int
    ) -> tuple[str | None, str | None]:
        """Resolve the GitHub GraphQL node ID and headRefName for a PR.

        Returns (node_id, head_ref_name) or (None, None) on failure.
        Per plan: failure → no command emitted, failure logged.
        """
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "id,headRefName",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            _log.error("gh pr view timed out for %s#%s", repo, pr_number)
            return None, None
        if proc.returncode != 0:
            _log.error(
                "gh pr view failed for %s#%s (rc=%s): %s",
                repo,
                pr_number,
                proc.returncode,
                stderr.decode(errors="replace"),
            )
            return None, None
        try:
            data: dict[str, Any] = json.loads(stdout)
            return data.get("id"), data.get("headRefName")
        except (json.JSONDecodeError, AttributeError) as exc:
            _log.error(
                "Failed to parse gh pr view output for %s#%s: %s", repo, pr_number, exc
            )
            return None, None

    async def _resolve_pr_refs(
        self, repo: str, pr_number: int
    ) -> tuple[str, str, str] | None:
        """Resolve headRefName, baseRefName, headRefOid for rebase command.

        Returns (head_ref, base_ref, head_oid) or None on failure.
        """
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "headRefName,baseRefName,headRefOid",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            _log.error("gh pr view refs timed out for %s#%s", repo, pr_number)
            return None
        if proc.returncode != 0:
            _log.error(
                "gh pr view refs failed for %s#%s: %s",
                repo,
                pr_number,
                stderr.decode(errors="replace"),
            )
            return None
        try:
            data: dict[str, Any] = json.loads(stdout)
            head_ref = data.get("headRefName", "")
            base_ref = data.get("baseRefName", "")
            head_oid = data.get("headRefOid", "")
            if not head_ref or not base_ref or not head_oid:
                _log.error("Missing ref fields for %s#%s: %r", repo, pr_number, data)
                return None
            return head_ref, base_ref, head_oid
        except (json.JSONDecodeError, AttributeError) as exc:
            _log.error(
                "Failed to parse gh pr view refs for %s#%s: %s", repo, pr_number, exc
            )
            return None

    async def _resolve_failing_run_id(self, repo: str, pr_number: int) -> str | None:
        """Find the most recent failing GitHub Actions run ID for a PR.

        Returns the run ID string or None if none found.
        """
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "statusCheckRollup",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            _log.error(
                "gh pr view statusCheckRollup timed out for %s#%s", repo, pr_number
            )
            return None
        if proc.returncode != 0:
            _log.error(
                "gh pr view statusCheckRollup failed for %s#%s: %s",
                repo,
                pr_number,
                stderr.decode(errors="replace"),
            )
            return None
        try:
            data: dict[str, Any] = json.loads(stdout)
            checks = data.get("statusCheckRollup") or []
            for check in checks:
                if check.get("conclusion") == "FAILURE":
                    details_url: str = str(check.get("detailsUrl") or "")
                    run_id = (
                        details_url.rstrip("/").split("/")[-1] if details_url else None
                    )
                    if run_id:
                        return run_id
            return None
        except (json.JSONDecodeError, AttributeError) as exc:
            _log.error(
                "Failed to parse statusCheckRollup for %s#%s: %s", repo, pr_number, exc
            )
            return None
