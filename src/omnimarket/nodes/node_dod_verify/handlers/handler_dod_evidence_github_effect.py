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

OMN-15709 (operator ruling R-b, 2026-08-05): ``FETCH_PR_CHECKS_GREEN`` is no
longer a verbatim ``gh pr checks --required`` carve-out. GitHub's Checks API
is keyed by commit SHA, not by PR — ``gh pr checks <n>`` resolves the PR's
head SHA and reports every check-run/check-suite attached to that SHA, so a
DIFFERENT PR sharing the same head SHA (e.g. a duplicate/superseding PR from
another branch) can pollute the rollup with foreign failures, permanently
reddening an already-MERGED, terminal PR's evidence. Empirically (OCC
#5745/#5749, same head SHA): ``check_suite.pull_requests[]`` is unpopulated
on 43/43 suites, so it cannot discriminate. The fix instead resolves the PR's
own ``headRefName`` via ``gh pr view``, enumerates check-suites for the head
SHA via ``gh api commits/{sha}/check-suites``, and filters check-runs (``gh
api commits/{sha}/check-runs``) to those whose ``check_suite.head_branch``
matches — a check-run attached to a suite on a DIFFERENT, resolvable branch
is provably foreign and excluded from the rollup; a check-run whose suite
cannot be resolved at all is treated as ambiguous and stays in the rollup
(fail-closed). Required-context names are read live from
``branches/{base}/protection/required_status_checks`` rather than trusted to
``gh pr checks --required``'s own filtering. See
``docs/tracking/ROLLING_WORK_LEDGER.md`` 2026-08-05 (ruling R-b) for the full
empirical trace this design is built from.
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
# QUEUED, IN_PROGRESS, ...) is treated as not-green — used by the remaining
# ``gh pr checks``-shaped lookups (none as of OMN-15709; kept for parity with
# historical fixtures/tests that still assert against this constant).
_GH_CHECK_GREEN_STATES = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})

# Raw Checks API ``conclusion`` values (lowercase) that are NOT a failure —
# OMN-15709's ``commits/{sha}/check-runs`` rollup carries ``status`` +
# ``conclusion`` separately rather than ``gh pr checks``' single normalized
# ``state`` string; a run only counts as green when ``status == "completed"``
# AND its conclusion is one of these.
_GH_CHECK_RUN_GREEN_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})

# OMN-15413 (AC6 dev-basis rescope, operator ruling R-l, 2026-08-05): the only
# branches this org ever configures required-status-checks on. A PR whose
# BASE branch is neither of these (a stacked PR landed on a feature branch)
# is expected to have ZERO required contexts — GitHub protection is
# per-branch and feature branches never carry it. Confined to exactly these
# two names so a genuinely misconfigured/renamed mainline branch still fails
# closed rather than silently widening the exemption.
_MAINLINE_BASE_BRANCHES = frozenset({"dev", "main"})

# Substring GitHub's ``gh api`` CLI emits on both 404 shapes this handler can
# hit while resolving branch protection for a non-mainline base branch:
# ``Branch not found`` (the branch itself no longer exists — deleted
# post-merge, the common case for a stacked PR's feature-branch base) and
# ``Branch not protected`` (the branch still exists but never carried
# protection, e.g. read immediately after merge, before cleanup). Both are a
# CONFIRMED, permanent absence of any requirement — never a transient/infra
# failure (timeout, auth, malformed JSON), whose ``_gh_json`` detail strings
# take a different shape entirely (``"{argv[:2]} error: {exc}"`` or a
# non-HTTP stderr line) and therefore never match this marker.
_CONFIRMED_BRANCH_ABSENCE_MARKER = "404"


def _required_check_names_from_classic(data: object) -> set[str]:
    """Extract required check names from classic branch protection.

    Older fixtures and GitHub's deprecated field expose a bare/list
    ``contexts`` shape. Current classic protection also exposes
    ``checks[].context``.
    """
    if isinstance(data, list):
        return {str(item) for item in data if isinstance(item, str) and item}
    if not isinstance(data, dict):
        return set()

    names = {
        str(item) for item in data.get("contexts", []) if isinstance(item, str) and item
    }
    checks = data.get("checks", [])
    if isinstance(checks, list):
        names.update(
            str(item["context"])
            for item in checks
            if isinstance(item, dict)
            and isinstance(item.get("context"), str)
            and item["context"]
        )
    return names


def _required_check_names_from_rules(data: object) -> set[str]:
    """Extract required check names from active branch rulesets."""
    if not isinstance(data, list):
        return set()
    names: set[str] = set()
    for rule in data:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        parameters = rule.get("parameters")
        if not isinstance(parameters, dict):
            continue
        checks = parameters.get("required_status_checks", [])
        if not isinstance(checks, list):
            continue
        names.update(
            str(item["context"])
            for item in checks
            if isinstance(item, dict)
            and isinstance(item.get("context"), str)
            and item["context"]
        )
    return names


def _gh_json(argv: list[str], timeout_s: int) -> tuple[object | None, str]:
    """Run a ``gh`` invocation and parse its stdout as a single JSON value.

    Returns ``(data, detail)``. ``data`` is ``None`` (never a falsy-but-valid
    JSON value gets confused with failure — callers check ``isinstance``) on
    timeout/OSError, non-zero exit, or unparseable stdout; ``detail`` carries
    a short diagnostic in that case, empty string otherwise.
    """
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"{argv[:2]} error: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return None, detail[:200] or f"non-zero exit ({result.returncode})"
    try:
        return json.loads(result.stdout or "null"), ""
    except json.JSONDecodeError:
        detail = (result.stderr or result.stdout or "").strip()
        return None, detail[:200] or "unparseable JSON"


def _gh_json_lines(
    argv: list[str], timeout_s: int
) -> tuple[list[dict[str, object]] | None, str]:
    """Run a ``gh api --paginate --jq '...[]'`` invocation whose stdout is
    JSON-lines (one JSON object per array element, one page's worth per
    invocation of the jq filter) rather than a single JSON document.

    Returns ``(items, detail)`` with the same ``None``-on-failure contract as
    :func:`_gh_json`. Non-dict lines are skipped (defensive only — the ``jq``
    projection always emits objects for the callers here); an unparseable
    line fails the whole call closed rather than silently dropping data.
    """
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"{argv[:2]} error: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return None, detail[:200] or f"non-zero exit ({result.returncode})"
    items: list[dict[str, object]] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None, f"unparseable line in paginated output: {line[:200]}"
        if isinstance(obj, dict):
            items.append(obj)
    return items, ""


def _is_green_check_run(run: dict[str, object]) -> bool:
    """A raw Checks-API check-run is green iff it completed with a
    non-failing conclusion — the two-field (``status``/``conclusion``)
    equivalent of ``gh pr checks``' single normalized ``state`` string."""
    status = str(run.get("status") or "").lower()
    conclusion = str(run.get("conclusion") or "").lower()
    return status == "completed" and conclusion in _GH_CHECK_RUN_GREEN_CONCLUSIONS


def _check_run_is_own_or_ambiguous(
    run: dict[str, object],
    pr_head_branch: str,
    suite_branch: dict[int, str | None],
) -> bool:
    """Whether ``run`` counts toward the PR's OWN evidence rollup.

    OMN-15709 (ruling R-b): ``check_suite.pull_requests[]`` is empty on real
    GitHub responses (43/43 in the OCC #5745/#5749 trace) and cannot
    discriminate, so branch attribution is done via the check-run's
    ``check_suite.id`` resolved against a ``commits/{sha}/check-suites``
    listing's ``head_branch``. A run is:

    * OWN — its suite resolves to a branch and that branch IS the PR's own
      ``headRefName``. Included.
    * AMBIGUOUS — its suite is missing from the listing, has no ``id``, or
      the listing carries a null/empty ``head_branch`` for it. Attribution
      cannot be proven either way, so this fails closed and is included
      (never silently excluded — AC2).
    * PROVABLY FOREIGN — its suite resolves to a DIFFERENT, known branch.
      Excluded: a different branch's PASS or FAIL must not influence this
      PR's own evidence.
    """
    suite = run.get("check_suite")
    suite_id = suite.get("id") if isinstance(suite, dict) else None
    if isinstance(suite_id, int) and suite_id in suite_branch:
        branch = suite_branch[suite_id]
        if branch is not None:
            return branch == pr_head_branch
    return True  # ambiguous: unresolved suite id or null head_branch


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
    # FETCH_PR_CHECKS_GREEN (OMN-15709 rewrite) — scoped to check-suites
    # whose ``head_branch`` is provably the PR's OWN branch (or whose branch
    # attribution is unresolvable, which fails closed), never a foreign
    # PR/branch sharing the same head SHA. See the module docstring for the
    # full empirical trace (OCC #5745/#5749). Required-context names are
    # read live from branch protection rather than trusted to
    # ``gh pr checks --required``'s own filtering.
    # ------------------------------------------------------------------
    def _fetch_pr_checks_green(
        self, command: ModelDodEvidenceGithubLookupCommand
    ) -> ModelDodEvidenceGithubLookupResultEvent:
        repo = command.repo or ""
        pr_number = command.pr_number or 0

        pr_data, pr_detail = _gh_json(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "headRefName,baseRefName,headRefOid",
            ],
            _GH_PR_TIMEOUT_S,
        )
        if not isinstance(pr_data, dict):
            return self._checks_not_green(
                command, f"could not resolve PR head/base branch: {pr_detail}"
            )
        head_branch = str(pr_data.get("headRefName") or "")
        base_branch = str(pr_data.get("baseRefName") or "")
        sha = str(pr_data.get("headRefOid") or "")
        if not head_branch or not base_branch or not sha:
            return self._checks_not_green(
                command,
                "gh pr view response missing headRefName/baseRefName/headRefOid",
            )

        classic_required, classic_detail = _gh_json(
            [
                "gh",
                "api",
                f"repos/{repo}/branches/{base_branch}/protection/required_status_checks",
            ],
            _GH_PR_TIMEOUT_S,
        )
        rules_required, rules_detail = _gh_json(
            [
                "gh",
                "api",
                f"repos/{repo}/rules/branches/{base_branch}",
            ],
            _GH_PR_TIMEOUT_S,
        )
        required_names = sorted(
            _required_check_names_from_classic(classic_required)
            | _required_check_names_from_rules(rules_required)
        )
        if not required_names:
            if (
                base_branch not in _MAINLINE_BASE_BRANCHES
                and _CONFIRMED_BRANCH_ABSENCE_MARKER in classic_detail
            ):
                # OMN-15413 (R-l): a stacked PR's non-mainline base branch has
                # no live-resolvable protection — either it never had any
                # (a plain feature branch) or it no longer exists (deleted
                # post-merge cleanup). Both are CONFIRMED terminal facts, not
                # an unresolvable/transient error, so treating this hop as
                # "not green" would be permanently and unfalsifiably RED for
                # an already-merged PR. Zero required contexts on a
                # non-mainline branch is vacuously satisfied; mainline
                # (dev/main) ancestry is proven by a separate check.
                return ModelDodEvidenceGithubLookupResultEvent(
                    correlation_id=command.correlation_id,
                    operation=command.operation,
                    checks_green=True,
                    detail=(
                        f"base branch {base_branch!r} is non-mainline and its "
                        f"required-status-checks are confirmed absent "
                        f"(classic={classic_detail}) rather than unresolvable "
                        f"— treated as vacuously satisfied for this hop; "
                        f"mainline (dev/main) ancestry must be proven by a "
                        f"separate check"
                    ),
                )
            return self._checks_not_green(
                command,
                f"could not resolve required status checks for {repo}@{base_branch}: "
                f"classic={classic_detail or 'no names'}; "
                f"rules={rules_detail or 'no names'}",
            )

        suites, suites_detail = _gh_json_lines(
            [
                "gh",
                "api",
                f"repos/{repo}/commits/{sha}/check-suites",
                "--paginate",
                "--jq",
                ".check_suites[] | {id, head_branch}",
            ],
            _GH_PR_TIMEOUT_S,
        )
        if suites is None:
            return self._checks_not_green(
                command,
                f"could not enumerate check-suites for {sha[:12]}: {suites_detail}",
            )
        suite_branch: dict[int, str | None] = {}
        for suite in suites:
            suite_id = suite.get("id")
            if isinstance(suite_id, int):
                head = suite.get("head_branch")
                suite_branch[suite_id] = (
                    str(head) if isinstance(head, str) and head else None
                )

        runs, runs_detail = _gh_json_lines(
            [
                "gh",
                "api",
                f"repos/{repo}/commits/{sha}/check-runs",
                "--paginate",
                "--jq",
                ".check_runs[] | {name, status, conclusion, check_suite}",
            ],
            _GH_PR_TIMEOUT_S,
        )
        if runs is None:
            return self._checks_not_green(
                command,
                f"could not enumerate check-runs for {sha[:12]}: {runs_detail}",
            )

        missing: list[str] = []
        foreign_only: list[str] = []
        not_green: list[str] = []
        evaluated_any = False
        for name in required_names:
            matches = [r for r in runs if str(r.get("name") or "") == name]
            if not matches:
                missing.append(name)
                continue
            relevant = [
                run
                for run in matches
                if _check_run_is_own_or_ambiguous(run, head_branch, suite_branch)
            ]
            if not relevant:
                # Every instance of this required context is PROVABLY foreign
                # (it ran only on a different, resolvable branch sharing this
                # SHA) — the PR's OWN branch never produced it. That is
                # functionally MISSING for this PR and fails closed: a
                # required context must not be satisfied (or reddened) by a
                # foreign PR/branch's runs. Silently `continue`-ing here
                # (dropping the context from the rollup entirely) would let a
                # foreign FAILURE flip an otherwise-correct RED into a false
                # GREEN by shrinking the set of contexts actually evaluated
                # — the exact fail-open hole this branch closes.
                foreign_only.append(name)
                continue
            evaluated_any = True
            if any(not _is_green_check_run(run) for run in relevant):
                not_green.append(name)

        if missing or foreign_only:
            absent = missing + foreign_only
            shown = ", ".join(absent[:10])
            more = "" if len(absent) <= 10 else f" (+{len(absent) - 10} more)"
            return self._checks_not_green(
                command,
                f"{len(absent)} required context(s) absent from "
                f"{head_branch}@{sha[:12]} (missing entirely or only "
                f"produced by a foreign branch sharing this SHA): "
                f"{shown}{more}",
            )
        if not_green:
            shown = ", ".join(not_green[:10])
            more = "" if len(not_green) <= 10 else f" (+{len(not_green) - 10} more)"
            return self._checks_not_green(
                command,
                f"{len(not_green)} required context(s) not green on "
                f"{head_branch}: {shown}{more}",
            )
        if not evaluated_any:
            return self._checks_not_green(
                command,
                f"no required context had an own-branch or unattributable "
                f"check-run on {sha[:12]}",
            )
        return ModelDodEvidenceGithubLookupResultEvent(
            correlation_id=command.correlation_id,
            operation=command.operation,
            checks_green=True,
            detail=(
                f"all {len(required_names)} required context(s) green for "
                f"{head_branch}@{sha[:12]}"
            ),
        )

    @staticmethod
    def _checks_not_green(
        command: ModelDodEvidenceGithubLookupCommand, detail: str
    ) -> ModelDodEvidenceGithubLookupResultEvent:
        return ModelDodEvidenceGithubLookupResultEvent(
            correlation_id=command.correlation_id,
            operation=command.operation,
            checks_green=False,
            detail=detail[:400],
        )


__all__ = ["HandlerDodEvidenceGithubEffect"]
