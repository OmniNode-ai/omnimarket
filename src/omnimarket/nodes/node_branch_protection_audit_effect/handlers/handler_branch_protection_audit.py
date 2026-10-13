# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure port of the change-control branch-protection auditor.

Checks review enforcement, role-dependent required contexts, release-sync push
restrictions, administrator enforcement, reporting App bindings, Receipt Gate,
Lane Identity Gate, branch deletion after merge, Merge Queue ruleset presence,
and the absence of bypass actors on enforced merge-queue rulesets.
"""

from __future__ import annotations

import json

from pydantic import JsonValue

from omnimarket.nodes.node_branch_protection_audit_effect.models import (
    EnumBranchProtectionCheckStatus,
    ModelBranchProtectionAuditRequest,
    ModelBranchProtectionAuditResult,
    ModelBranchProtectionFinding,
    protection_key,
    repo_key,
    review_rules_key,
    ruleset_key,
    rulesets_key,
)

Status = EnumBranchProtectionCheckStatus


def _field(value: JsonValue, *keys: str) -> JsonValue:
    """jq object traversal: absent keys and traversal through null yield null."""
    for key in keys:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("cannot index this JSON value with a string")
        value = value.get(key)
    return value


def _array(value: JsonValue) -> list[JsonValue]:
    """Array-valued ``.x // []``; false and null take the alternative."""
    if value is None or value is False:
        return []
    if not isinstance(value, list):
        raise ValueError("expected a JSON array")
    return value


def _entries(value: JsonValue) -> list[JsonValue]:
    """jq ``.[]?`` iterates arrays and object values, skipping scalars."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _tostring(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _actors(value: JsonValue, *, missing_dash: bool = False) -> str:
    actors = []
    for actor in _entries(value):
        parts = []
        for key in ("actor_type", "actor_id", "bypass_mode"):
            part = _field(actor, key)
            if missing_dash and (part is None or part is False):
                part = "-"
            parts.append(_tostring(part))
        actors.append(":".join(parts))
    return ",".join(actors)


def _merge_queue_bypass_verdict(detail: JsonValue) -> str:
    """Match the script's verdict, including malformed or unreadable details."""
    if not isinstance(detail, dict) or "rules" not in detail:
        return "UNREADABLE"
    if detail.get("enforcement") == "disabled":
        return "NOT_QUEUE"
    try:
        rule_types = [_field(rule, "type") for rule in _entries(detail["rules"])]
        if "merge_queue" not in rule_types:
            return "NOT_QUEUE"
        actors = detail.get("bypass_actors")
        if actors is None:
            return "UNREADABLE"
        if isinstance(actors, (list, dict, str)) and len(actors) == 0:
            return "NO_BYPASS"
        if (
            isinstance(actors, (int, float))
            and not isinstance(actors, bool)
            and actors == 0
        ):
            return "NO_BYPASS"
        if not isinstance(actors, (list, dict)):
            return "UNREADABLE"
        return "BYPASS " + _actors(actors, missing_dash=True)
    except ValueError:
        return "UNREADABLE"


def _contexts(protection: JsonValue) -> list[JsonValue]:
    return _array(_field(protection, "required_status_checks", "contexts")) + [
        _field(check, "context")
        for check in _array(_field(protection, "required_status_checks", "checks"))
    ]


class _Audit:
    """Per-call report builder; no mutable state escapes into the handler."""

    def __init__(self, request: ModelBranchProtectionAuditRequest) -> None:
        self.request = request
        self.lines: list[str] = []
        self.findings: list[ModelBranchProtectionFinding] = []
        self.total_checks = 0
        self.failures = 0
        self.noncompliant_repos: list[str] = []

    def _record(
        self,
        repo: str,
        branch: str,
        check: str,
        status: Status,
        detail: str,
        message: str,
        *,
        counted: bool = True,
    ) -> None:
        if counted:
            self.total_checks += 1
        if status == Status.FAIL:
            self.failures += 1
        prefix = "  [repo]" if branch == "-" else f"    [{branch}]"
        self.lines.append(f"{prefix} {status.value}: {message}")
        self.findings.append(
            ModelBranchProtectionFinding(
                repo=repo,
                branch=branch,
                check=check,
                status=status,
                detail=detail,
            )
        )

    def _judge(
        self,
        repo: str,
        branch: str,
        check: str,
        passed: bool,
        pass_detail: str,
        fail_detail: str,
        pass_message: str,
        fail_message: str,
    ) -> None:
        self._record(
            repo,
            branch,
            check,
            Status.PASS if passed else Status.FAIL,
            pass_detail if passed else fail_detail,
            pass_message if passed else fail_message,
        )

    def _api_response(self, key: str, *, branch: bool) -> None:
        indent = "             " if branch else "         "
        self.lines.append(
            f"{indent}API response: {self.request.snapshot.errors.get(key, '')}"
        )

    def _check_release_synced_main(self, repo: str, protection: JsonValue) -> None:
        try:
            count = len(
                {
                    json.dumps(context, sort_keys=True, ensure_ascii=False)
                    for context in _contexts(protection)
                }
            )
        except ValueError:
            count = -1
        self._judge(
            repo,
            "main",
            "release_synced_contexts_empty",
            count == 0,
            "0 required contexts",
            f"{count} required contexts",
            "required_status_checks is empty (release-synced main)",
            f"release-synced main carries {count} required status check(s) — a PR context cannot report on an automated fast-forward ref, it only blocks the sync",
        )
        documents = self.request.snapshot.documents
        key = rulesets_key(self.request.org, repo)
        if key not in documents:
            self._record(
                repo,
                "main",
                "release_synced_push_ruleset",
                Status.FAIL,
                "rulesets not fetchable",
                "could not fetch rulesets",
            )
            self._api_response(key, branch=True)
            return
        for candidate in _entries(documents[key]):
            if (
                not isinstance(candidate, dict)
                or candidate.get("enforcement") != "active"
                or candidate.get("target") != "branch"
            ):
                continue
            rid = _tostring(candidate.get("id"))
            key = ruleset_key(self.request.org, repo, rid)
            if key not in documents:
                continue
            detail = documents[key]
            try:
                includes = _array(_field(detail, "conditions", "ref_name", "include"))
                excludes = _array(_field(detail, "conditions", "ref_name", "exclude"))
                rule_types = [
                    _field(rule, "type") for rule in _entries(_field(detail, "rules"))
                ]
                actors = _field(detail, "bypass_actors")
                if not any(
                    ref in ("refs/heads/main", "~DEFAULT_BRANCH") for ref in includes
                ):
                    continue
                if "refs/heads/main" in excludes or "update" not in rule_types:
                    continue
                if not _entries(actors):
                    continue
                verdict = _actors(actors)
            except ValueError:
                continue
            if verdict and rid:
                self._record(
                    repo,
                    "main",
                    "release_synced_push_ruleset",
                    Status.PASS,
                    f"ruleset {rid}; bypass {verdict}",
                    f"active ruleset {rid} restricts updates to refs/heads/main (bypass actors: {verdict})",
                )
                return
        self._record(
            repo,
            "main",
            "release_synced_push_ruleset",
            Status.FAIL,
            "no active update-restricting ruleset with bypass actors",
            "no active ruleset restricts updates to refs/heads/main with a non-empty bypass-actor set",
        )
        self.lines.append(
            "             main has no required contexts AND no push restriction — it is unprotected."
        )

    def _check_reviews(self, repo: str, branch: str) -> None:
        rules = self.request.snapshot.documents.get(
            review_rules_key(self.request.org, repo)
        )
        try:
            nodes = _entries(
                _field(rules, "data", "repository", "branchProtectionRules", "nodes")
            )
            matched = next(
                (node for node in nodes if _field(node, "pattern") == branch), None
            )
            approving = (
                _tostring(_field(matched, "requiresApprovingReviews"))
                if matched is not None
                else "unknown"
            )
            owners = (
                _tostring(_field(matched, "requiresCodeOwnerReviews"))
                if matched is not None
                else "unknown"
            )
        except ValueError:
            matched = None
            approving = owners = "unknown"
        if branch == "main" and repo in self.request.policy.review_gated_main_repos:
            state = f"{approving}:{owners}" if matched is not None else "unknown"
            self._judge(
                repo,
                branch,
                "reviews_required",
                state == "true:true",
                "requiresApprovingReviews=true; requiresCodeOwnerReviews=true",
                f"requiresApprovingReviews/requiresCodeOwnerReviews={state}",
                "approving and code-owner reviews are enforced (review-gated main)",
                f"review-gated main requires approving and code-owner reviews (GraphQL state={state})",
            )
        elif approving in ("false", "true"):
            self._judge(
                repo,
                branch,
                "reviews_not_enforced",
                approving == "false",
                "requiresApprovingReviews=false",
                "requiresApprovingReviews=true",
                "approving reviews not enforced (GraphQL requiresApprovingReviews=false)",
                "approving reviews are enforced (blocks solo-dev merges)",
            )
        else:
            self._record(
                repo,
                branch,
                "reviews_not_enforced",
                Status.WARN,
                "GraphQL rule unavailable",
                "could not determine review enforcement via GraphQL (no matching rule)",
            )

    def _check_app_binding(self, repo: str, branch: str, protection: JsonValue) -> None:
        checks = _array(_field(protection, "required_status_checks", "checks"))
        unbound = ", ".join(
            ""
            if _field(check, "context") is None
            else _tostring(_field(check, "context"))
            for check in checks
            if _field(check, "app_id") is None
        )
        self._judge(
            repo,
            branch,
            "required_check_app_binding",
            not unbound,
            "all bound",
            f"unbound contexts: {unbound}",
            "every required status check is bound to a reporting App",
            f'required status check(s) with app_id=null ("Any source" -- satisfiable by any App, not just the intended CI producer): {unbound}',
        )

    def _check_receipt_gate(
        self, repo: str, branch: str, contexts: list[JsonValue]
    ) -> None:
        if branch == "main" and repo in self.request.policy.release_synced_main_repos:
            self._record(
                repo,
                branch,
                "required_check_receipt_gate",
                Status.SKIP,
                "release-synced main",
                '"verify / verify" Receipt Gate not asserted on release-synced main (no PR context can report there)',
                counted=False,
            )
        elif repo in self.request.policy.receipt_gate_required_repos:
            present = "verify / verify" in contexts
            if branch == "main":
                self._judge(
                    repo,
                    branch,
                    "required_check_receipt_gate",
                    present,
                    "verify / verify required",
                    "verify / verify missing",
                    '"verify / verify" Receipt Gate is a required status check',
                    '"verify / verify" Receipt Gate not found in required status checks',
                )
            else:
                self._record(
                    repo,
                    branch,
                    "required_check_receipt_gate",
                    Status.NOTE,
                    "present; dev not asserted"
                    if present
                    else "absent; dev not asserted (flagged)",
                    '"verify / verify" Receipt Gate present (dev not asserted — informational)'
                    if present
                    else '"verify / verify" Receipt Gate absent on dev (dev not asserted — flagged for operator, OMN-14683)',
                    counted=False,
                )

    def _check_branch(self, repo: str, branch: str) -> None:
        self.lines.append(f"  ── branch: {branch} ───────────────────")
        key = protection_key(self.request.org, repo, branch)
        documents = self.request.snapshot.documents
        if key not in documents:
            self._record(
                repo,
                branch,
                "branch_protection_fetch",
                Status.FAIL,
                "protection not fetchable",
                "Could not fetch branch protection (is it enabled?)",
                counted=False,
            )
            self._api_response(key, branch=True)
            return
        protection = documents[key]
        self._check_reviews(repo, branch)
        if branch == "main" and repo in self.request.policy.release_synced_main_repos:
            self._check_release_synced_main(repo, protection)
        else:
            self._judge(
                repo,
                branch,
                "required_check_ci_summary",
                "CI Summary" in _contexts(protection),
                "CI Summary required",
                "CI Summary missing",
                '"CI Summary" is a required status check',
                '"CI Summary" not found in required status checks',
            )
        enforce = _field(protection, "enforce_admins", "enabled")
        self._judge(
            repo,
            branch,
            "enforce_admins",
            _tostring(enforce) == "true",
            "enabled",
            "disabled",
            "enforce_admins is enabled",
            "enforce_admins is not enabled",
        )
        self._check_app_binding(repo, branch, protection)
        contexts = _contexts(protection)
        self._check_receipt_gate(repo, branch, contexts)
        if (
            branch == "dev"
            and repo in self.request.policy.lane_identity_gate_required_repos
        ):
            self._judge(
                repo,
                branch,
                "required_check_lane_identity_gate",
                "Lane Identity Gate" in contexts,
                "Lane Identity Gate required",
                "Lane Identity Gate missing",
                '"Lane Identity Gate" is a required status check',
                '"Lane Identity Gate" not found in required status checks — the gate reports but cannot block (OMN-18288)',
            )

    def _check_merge_queue_bypass(self, repo: str, rulesets: JsonValue) -> None:
        judged = False
        for ruleset in _entries(rulesets):
            if _field(ruleset, "enforcement") == "disabled":
                continue
            rid = _tostring(_field(ruleset, "id"))
            key = ruleset_key(self.request.org, repo, rid)
            if key not in self.request.snapshot.documents:
                self._record(
                    repo,
                    "-",
                    "merge_queue_ruleset_no_bypass",
                    Status.FAIL,
                    f"ruleset {rid} detail not fetchable",
                    f"ruleset {rid} is unreadable, so whether it lets anyone merge outside the queue is unknown",
                    counted=False,
                )
                judged = True
                continue
            verdict = _merge_queue_bypass_verdict(self.request.snapshot.documents[key])
            if verdict == "NOT_QUEUE":
                continue
            judged = True
            if verdict == "NO_BYPASS":
                self._record(
                    repo,
                    "-",
                    "merge_queue_ruleset_no_bypass",
                    Status.PASS,
                    f"ruleset {rid}",
                    f"merge-queue ruleset {rid} has no bypass actor",
                    counted=False,
                )
            elif verdict.startswith("BYPASS "):
                actors = verdict.removeprefix("BYPASS ")
                self._record(
                    repo,
                    "-",
                    "merge_queue_ruleset_no_bypass",
                    Status.FAIL,
                    f"ruleset {rid}; bypass {actors}",
                    f"merge-queue ruleset {rid} has bypass actor(s) {actors} -- that identity can merge outside the queue",
                    counted=False,
                )
            else:
                self._record(
                    repo,
                    "-",
                    "merge_queue_ruleset_no_bypass",
                    Status.FAIL,
                    f"ruleset {rid} bypass list unreadable",
                    f"the bypass list of ruleset {rid} is unreadable (no bypass_actors in the response; the token cannot administer the repo), so the absence of a bypass cannot be claimed",
                    counted=False,
                )
        if not judged:
            self._record(
                repo,
                "-",
                "merge_queue_ruleset_no_bypass",
                Status.PASS,
                "no enforced merge-queue ruleset",
                "no enforced merge-queue ruleset (nothing to bypass)",
                counted=False,
            )

    def _check_repo_settings(self, repo: str) -> None:
        key = repo_key(self.request.org, repo)
        documents = self.request.snapshot.documents
        if key not in documents:
            self._record(
                repo,
                "-",
                "repo_settings_fetch",
                Status.FAIL,
                "repo settings not fetchable",
                "Could not fetch repo settings",
            )
            self._api_response(key, branch=False)
        else:
            self._judge(
                repo,
                "-",
                "delete_branch_on_merge",
                _tostring(_field(documents[key], "delete_branch_on_merge")) == "true",
                "true",
                "not true",
                "delete_branch_on_merge is true",
                "delete_branch_on_merge is not true",
            )

    def _check_merge_queue(self, repo: str) -> None:
        if repo in self.request.policy.private_repos:
            self.lines.append("  [repo] SKIP: Merge Queue ruleset check (private repo)")
            return
        key = rulesets_key(self.request.org, repo)
        documents = self.request.snapshot.documents
        if key not in documents:
            self._record(
                repo,
                "-",
                "merge_queue_ruleset",
                Status.FAIL,
                "rulesets not fetchable",
                "Could not fetch rulesets",
            )
            self._api_response(key, branch=False)
            return
        rulesets = documents[key]
        self._judge(
            repo,
            "-",
            "merge_queue_ruleset",
            any(_field(rs, "name") == "Merge Queue" for rs in _entries(rulesets)),
            "exists",
            "not found",
            '"Merge Queue" ruleset exists',
            '"Merge Queue" ruleset not found',
        )
        self.total_checks += 1
        self._check_merge_queue_bypass(repo, rulesets)

    def _check_repo(self, repo: str) -> None:
        initial_failures = self.failures
        self.lines.extend(
            [
                "───────────────────────────────────────",
                f"Repo: {self.request.org}/{repo}",
                "───────────────────────────────────────",
            ]
        )
        for branch in self.request.branches:
            if branch == "main" and repo in self.request.policy.main_audit_exempt_repos:
                self.lines.append("  ── branch: main ───────────────────")
                self._record(
                    repo,
                    branch,
                    "main_branch_audit",
                    Status.SKIP,
                    "main-audit-exempt (temporary repo)",
                    "main not audited (temporary repo — excluded from branch-protection hardening by operator ruling 2026-08-21; see OMN-17186)",
                    counted=False,
                )
            elif branch == "dev" and repo in self.request.policy.dev_exempt_repos:
                self.lines.append("  ── branch: dev ───────────────────")
                self._record(
                    repo,
                    branch,
                    "dev_branch_protection",
                    Status.SKIP,
                    "dev-exempt (no protected dev branch)",
                    "repo has no protected dev branch (dev-exempt)",
                    counted=False,
                )
            else:
                self._check_branch(repo, branch)
        self._check_repo_settings(repo)
        self._check_merge_queue(repo)
        if self.failures == initial_failures:
            self.lines.append("  >>> COMPLIANT")
        else:
            self.noncompliant_repos.append(repo)
            self.lines.append("  >>> NON-COMPLIANT")
        self.lines.append("")

    def run(self) -> ModelBranchProtectionAuditResult:
        policy = self.request.policy
        separator = "======================================="
        self.lines.extend(
            [
                separator,
                " Branch Protection Audit",
                f" Org: {self.request.org}  |  Branches: {' '.join(self.request.branches)}",
                f" Dev-exempt (main only): {' '.join(policy.dev_exempt_repos)}",
                f" Main-exempt (dev only): {' '.join(policy.main_audit_exempt_repos)}",
                f" Release-synced main:    {' '.join(policy.release_synced_main_repos)}",
                f" Date: {self.request.audited_at}",
                separator,
                "",
            ]
        )
        for repo in policy.repos:
            self._check_repo(repo)
        self.lines.extend(
            [
                separator,
                f" Summary: {self.failures} failure(s) across {self.total_checks} checks",
                separator,
            ]
        )
        if self.failures == 0:
            self.lines.append("All repos compliant.")
        return ModelBranchProtectionAuditResult(
            exit_code=1 if self.failures else 0,
            failures=self.failures,
            total_checks=self.total_checks,
            findings=tuple(self.findings),
            noncompliant_repos=tuple(self.noncompliant_repos),
            report="\n".join(self.lines) + "\n",
        )


class HandlerBranchProtectionAudit:
    """Judge an immutable request without I/O, a clock, or environment access."""

    def handle(
        self, request: ModelBranchProtectionAuditRequest
    ) -> ModelBranchProtectionAuditResult:
        return _Audit(request).run()
