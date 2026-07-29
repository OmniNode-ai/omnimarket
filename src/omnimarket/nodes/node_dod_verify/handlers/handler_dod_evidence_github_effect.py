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
import re
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


def _exact_ticket_token_candidates(
    items: object, ticket_id: str
) -> list[dict[str, object]]:
    """Filter ``gh pr list`` candidates to an EXACT ticket-id token match.

    OMN-15382: the prior implementation trusted ``gh pr list --search
    <ticket_id>``'s fuzzy full-text ranking and blindly took ``.[0]`` — a
    similarly-worded PR for a DIFFERENT ticket could rank first and get bound
    silently. Here every candidate's ``title``/``headRefName`` must contain
    the ticket id as a whole token (not a digit-adjacent substring, so a
    shorter ticket id sharing the same leading digits does not match a
    longer one) before it is trusted at all.
    """
    if not isinstance(items, list):
        return []
    # CodeRabbit (PR #1949): branch names are conventionally lowercased
    # (``jonah/omn-13996-x``) while ``ticket_id`` arrives uppercase, so a
    # case-sensitive pattern only ever matches the ``title`` arm in
    # practice — the ``headRefName`` signal was effectively dead. Match
    # case-insensitively; the token-boundary lookaround still prevents a
    # shorter ticket id matching inside a longer one.
    pattern = re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(ticket_id)}(?!\d)", re.IGNORECASE
    )
    matches: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        head_ref = str(item.get("headRefName") or "")
        if pattern.search(title) or pattern.search(head_ref):
            matches.append(item)
    return matches


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
    # LOOKUP_PR_FOR_TICKET (OMN-15382 fail-closed rewrite) — the PR_NUMBER
    # env short-circuit is not gh I/O and stays in EvidenceCollector.
    #
    # Root cause fixed here: the prior implementation ran
    # ``gh pr list --search <ticket_id>`` with NO ``--repo`` flag (so it
    # silently resolved whatever repo the process cwd's git remote pointed
    # at) and blindly took ``.[0]`` of a fuzzy full-text search — this is how
    # ``${PR_NUMBER}`` bound PR #2454 instead of the correct #2536. Now:
    # ``command.repo`` is REQUIRED (never guessed here — EvidenceCollector
    # resolves it from an authoritative source before calling), and every
    # candidate must contain the ticket id as an exact token in its title or
    # branch name; zero or more than one surviving candidate is a fail-closed
    # RED (``PR_LOOKUP_FAILED`` / ``PR_LOOKUP_AMBIGUOUS``), never a silent
    # "most recent" pick.
    # ------------------------------------------------------------------
    def _lookup_pr_for_ticket(
        self, command: ModelDodEvidenceGithubLookupCommand
    ) -> ModelDodEvidenceGithubLookupResultEvent:
        ticket_id = command.ticket_id or ""
        repo = command.repo or ""
        if not repo:
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                text_value="",
                error_code="PR_LOOKUP_FAILED",
            )
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    repo,
                    "--search",
                    ticket_id,
                    "--state",
                    "merged",
                    "--json",
                    "number,title,headRefName",
                ],
                capture_output=True,
                text=True,
                timeout=_GH_LIST_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("gh pr list failed for %s (%s): %s", ticket_id, repo, exc)
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                text_value="",
                error_code="PR_LOOKUP_FAILED",
            )
        if result.returncode != 0:
            logger.warning(
                "gh pr list non-zero for %s (%s): %s",
                ticket_id,
                repo,
                result.stderr.strip(),
            )
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                text_value="",
                error_code="PR_LOOKUP_FAILED",
            )
        try:
            data = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                text_value="",
                error_code="PR_LOOKUP_FAILED",
            )
        candidates = _exact_ticket_token_candidates(data, ticket_id)
        if len(candidates) != 1:
            code = "PR_LOOKUP_AMBIGUOUS" if len(candidates) > 1 else "PR_LOOKUP_FAILED"
            logger.warning(
                "PR lookup for %s in %s resolved %d exact-token candidate(s) "
                "(%s) — failing closed instead of guessing.",
                ticket_id,
                repo,
                len(candidates),
                code,
            )
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                text_value="",
                error_code=code,
            )
        number = candidates[0].get("number")
        if not isinstance(number, int) or number <= 0:
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                text_value="",
                error_code="PR_LOOKUP_FAILED",
            )
        return ModelDodEvidenceGithubLookupResultEvent(
            correlation_id=command.correlation_id,
            operation=command.operation,
            text_value=str(number),
        )

    # ------------------------------------------------------------------
    # LOOKUP_REPO_FOR_TICKET (OMN-15382 hardening) — same exact-token
    # fail-closed discipline as LOOKUP_PR_FOR_TICKET above. This lookup
    # cannot itself take a ``--repo`` flag (finding the repo is the point),
    # so it remains scoped by whatever repo the invoking process's cwd git
    # remote resolves to — a residual limitation callers must not treat as
    # cross-repo discovery (see EvidenceCollector._lookup_pr_for_ticket,
    # which no longer depends on this method to source ``--repo``).
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
                    "number,title,headRefName,headRepository",
                ],
                capture_output=True,
                text=True,
                timeout=_GH_LIST_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("gh pr list failed for %s: %s", ticket_id, exc)
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                text_value="",
                error_code="REPO_LOOKUP_FAILED",
            )
        if result.returncode != 0:
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                text_value="",
                error_code="REPO_LOOKUP_FAILED",
            )
        try:
            data = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                text_value="",
                error_code="REPO_LOOKUP_FAILED",
            )
        candidates = _exact_ticket_token_candidates(data, ticket_id)
        # CodeRabbit (PR #1949): this lookup only needs a unique *repository*,
        # not a unique PR — a ticket with two merged PRs in the same repo
        # (common: a follow-up fix PR) is not repo-ambiguous. Collapse to the
        # distinct ``nameWithOwner`` set before judging cardinality.
        repos = sorted(
            {
                str(head.get("nameWithOwner") or "")
                for candidate in candidates
                if isinstance(head := candidate.get("headRepository"), dict)
                and head.get("nameWithOwner")
            }
        )
        if len(repos) != 1:
            code = "REPO_LOOKUP_AMBIGUOUS" if len(repos) > 1 else "REPO_LOOKUP_FAILED"
            return ModelDodEvidenceGithubLookupResultEvent(
                correlation_id=command.correlation_id,
                operation=command.operation,
                text_value="",
                error_code=code,
            )
        return ModelDodEvidenceGithubLookupResultEvent(
            correlation_id=command.correlation_id,
            operation=command.operation,
            text_value=repos[0],
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
