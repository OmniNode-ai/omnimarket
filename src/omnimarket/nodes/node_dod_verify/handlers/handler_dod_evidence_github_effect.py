# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerDodEvidenceGithubEffect — canonical EFFECT handler for node_dod_verify's
live-GitHub evidence lookups.

OMN-14400 (RSD-1 of OMN-14398): carves the 4 gh-CLI subprocess methods out of
the bespoke ``EvidenceCollector`` "service" class (CLAUDE.md rule 7a: live I/O
belongs in a HANDLER, never a freestanding service) into a canonical EFFECT
handler. Behavior is unchanged: the exact same ``gh`` CLI invocations and JSON
parsing that ``EvidenceCollector`` ran directly now run here;
``EvidenceCollector`` delegates to this handler instead of shelling `gh`
itself.

Kept as subprocess-based ``gh`` CLI calls (not re-implemented against the raw
GitHub REST API) to guarantee behavior parity — ``gh pr checks``'
green/not-green bucketing is `gh`'s own aggregation over the Checks + Commit
Status APIs, and re-deriving it from the raw REST surface is a nontrivial
redesign, out of scope for a behavior-identical carve-out.
``node_ci_watch``'s handler is an existing precedent for subprocess-based
``gh`` calls living inside a canonical handler.

RSD-2/RSD-3 (tracked under the OMN-14398 epic) carve the remaining
contract-loading/check-execution I/O and retire
``services/evidence_collector.py`` entirely, including wiring this handler's
operation into ``node_dod_verify``'s node-level archetype/purity declaration.
This handler intentionally does NOT touch ``HandlerDodVerify`` or the
``_make_collector`` seam that OMN-14392 (#1719)'s hermetic test depends on.
"""

from __future__ import annotations

import json
import logging
import subprocess
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_dod_verify.models.model_dod_evidence_github_lookup import (
    EnumDodEvidenceGithubOperation,
    ModelDodEvidenceGithubLookupCommand,
    ModelDodEvidenceGithubLookupResultEvent,
)

logger = logging.getLogger(__name__)

_GH_LIST_TIMEOUT_S = 15
_GH_PR_TIMEOUT_S = 30
_HANDLER_ID = "node_dod_verify.dod_evidence_github_effect"

# ``gh pr checks --json ... state`` values that are NOT a failure. Anything
# else (FAILURE, CANCELLED, ERROR, TIMED_OUT, ACTION_REQUIRED, PENDING,
# QUEUED, IN_PROGRESS, ...) is treated as not-green — carried over verbatim
# from EvidenceCollector._fetch_pr_checks_green.
_GH_CHECK_GREEN_STATES = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})


class HandlerDodEvidenceGithubEffect:
    """EFFECT: live ``gh`` CLI lookups for dod_verify's evidence collection.

    Behavior-identical carve-out of ``EvidenceCollector``'s
    ``_lookup_pr_for_ticket`` / ``_lookup_repo_for_ticket`` /
    ``_fetch_pr_merge_state`` / ``_fetch_pr_checks_green``.
    """

    def handle(
        self, command: ModelDodEvidenceGithubLookupCommand
    ) -> ModelHandlerOutput[None]:
        """Dispatch to the requested gh-CLI lookup and emit its result."""
        result_event = self._dispatch(command)
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=command.correlation_id,
            handler_id=_HANDLER_ID,
            events=(result_event,),
        )

    def _dispatch(
        self, command: ModelDodEvidenceGithubLookupCommand
    ) -> ModelDodEvidenceGithubLookupResultEvent:
        if command.operation == EnumDodEvidenceGithubOperation.LOOKUP_PR_FOR_TICKET:
            return self._lookup_pr_for_ticket(command)
        if command.operation == EnumDodEvidenceGithubOperation.LOOKUP_REPO_FOR_TICKET:
            return self._lookup_repo_for_ticket(command)
        if command.operation == EnumDodEvidenceGithubOperation.FETCH_PR_MERGE_STATE:
            return self._fetch_pr_merge_state(command)
        if command.operation == EnumDodEvidenceGithubOperation.FETCH_PR_CHECKS_GREEN:
            return self._fetch_pr_checks_green(command)
        raise ValueError(f"Unknown operation: {command.operation!r}")

    # ------------------------------------------------------------------
    # LOOKUP_PR_FOR_TICKET — verbatim carve-out of
    # EvidenceCollector._lookup_pr_for_ticket (the PR_NUMBER env
    # short-circuit is not gh I/O and stays in EvidenceCollector).
    # ------------------------------------------------------------------
    def _lookup_pr_for_ticket(
        self, command: ModelDodEvidenceGithubLookupCommand
    ) -> ModelDodEvidenceGithubLookupResultEvent:
        ticket_id = command.ticket_id or ""
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--search",
                    ticket_id,
                    "--state",
                    "merged",
                    "--json",
                    "number",
                    "--jq",
                    ".[0].number",
                ],
                capture_output=True,
                text=True,
                timeout=_GH_LIST_TIMEOUT_S,
            )
            if result.returncode == 0:
                num = result.stdout.strip()
                if num and num != "null":
                    return ModelDodEvidenceGithubLookupResultEvent(
                        correlation_id=command.correlation_id,
                        operation=command.operation,
                        text_value=num,
                    )
        except Exception:
            pass
        return ModelDodEvidenceGithubLookupResultEvent(
            correlation_id=command.correlation_id,
            operation=command.operation,
            text_value="",
        )

    # ------------------------------------------------------------------
    # LOOKUP_REPO_FOR_TICKET — verbatim carve-out of
    # EvidenceCollector._lookup_repo_for_ticket.
    # ------------------------------------------------------------------
    def _lookup_repo_for_ticket(
        self, command: ModelDodEvidenceGithubLookupCommand
    ) -> ModelDodEvidenceGithubLookupResultEvent:
        ticket_id = command.ticket_id or ""
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--search",
                    ticket_id,
                    "--state",
                    "merged",
                    "--json",
                    "number,headRepository",
                    "--jq",
                    '.[0] | .headRepository.nameWithOwner // ""',
                ],
                capture_output=True,
                text=True,
                timeout=_GH_LIST_TIMEOUT_S,
            )
            if result.returncode == 0:
                repo = result.stdout.strip()
                if repo and repo != "null":
                    return ModelDodEvidenceGithubLookupResultEvent(
                        correlation_id=command.correlation_id,
                        operation=command.operation,
                        text_value=repo,
                    )
        except Exception:
            pass
        return ModelDodEvidenceGithubLookupResultEvent(
            correlation_id=command.correlation_id,
            operation=command.operation,
            text_value="",
        )

    # ------------------------------------------------------------------
    # FETCH_PR_MERGE_STATE — verbatim carve-out of
    # EvidenceCollector._fetch_pr_merge_state.
    # ------------------------------------------------------------------
    def _fetch_pr_merge_state(
        self, command: ModelDodEvidenceGithubLookupCommand
    ) -> ModelDodEvidenceGithubLookupResultEvent:
        repo = command.repo or ""
        pr_number = command.pr_number or 0
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    repo,
                    "--json",
                    "state,mergedAt",
                ],
                capture_output=True,
                text=True,
                timeout=_GH_PR_TIMEOUT_S,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("gh pr view failed for %s#%d: %s", repo, pr_number, exc)
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                resolved=False,
            )
        if result.returncode != 0:
            logger.warning(
                "gh pr view non-zero for %s#%d: %s",
                repo,
                pr_number,
                result.stderr.strip(),
            )
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                resolved=False,
            )
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            logger.warning(
                "gh pr view returned unparseable JSON for %s#%d", repo, pr_number
            )
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                resolved=False,
            )
        if not isinstance(data, dict):
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                resolved=False,
            )
        state = str(data.get("state") or "UNKNOWN")
        merged = bool(data.get("mergedAt")) or state.upper() == "MERGED"
        return ModelDodEvidenceGithubLookupResultEvent(
            correlation_id=command.correlation_id,
            operation=command.operation,
            merged=merged,
            state=state,
        )

    # ------------------------------------------------------------------
    # FETCH_PR_CHECKS_GREEN — verbatim carve-out of
    # EvidenceCollector._fetch_pr_checks_green. Scoped to required checks
    # only via ``gh pr checks --required`` (OMN-14390) — a non-green
    # *non-required* check (e.g. an informational/advisory job) must never
    # fail a Done-flip; only branch-protection-required contexts are
    # load-bearing here.
    # ------------------------------------------------------------------
    def _fetch_pr_checks_green(
        self, command: ModelDodEvidenceGithubLookupCommand
    ) -> ModelDodEvidenceGithubLookupResultEvent:
        repo = command.repo or ""
        pr_number = command.pr_number or 0
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "checks",
                    str(pr_number),
                    "--repo",
                    repo,
                    "--required",
                    "--json",
                    "name,state",
                ],
                capture_output=True,
                text=True,
                timeout=_GH_PR_TIMEOUT_S,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                checks_green=False,
                detail=f"gh pr checks error: {exc}",
            )
        try:
            data = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            detail = (result.stderr or result.stdout or "").strip()
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                checks_green=False,
                detail=f"could not read check results: {detail[:200]}",
            )
        if not isinstance(data, list) or not data:
            detail = (result.stderr or "no status checks reported").strip()
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                checks_green=False,
                detail=f"no status checks reported: {detail[:200]}",
            )
        not_green = sorted(
            str(check.get("name") or "<unnamed>")
            for check in data
            if isinstance(check, dict)
            and str(check.get("state") or "").upper() not in _GH_CHECK_GREEN_STATES
        )
        if not_green:
            shown = ", ".join(not_green[:10])
            more = "" if len(not_green) <= 10 else f" (+{len(not_green) - 10} more)"
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                checks_green=False,
                detail=f"{len(not_green)} check(s) not green: {shown}{more}",
            )
        return ModelDodEvidenceGithubLookupResultEvent(
            correlation_id=command.correlation_id,
            operation=command.operation,
            checks_green=True,
            detail=f"all {len(data)} status check(s) green",
        )


__all__ = ["HandlerDodEvidenceGithubEffect"]
