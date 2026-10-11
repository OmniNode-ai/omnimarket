# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay supplied ledger facts and select lab-fill work without I/O (OMN-20662).

OMN-20864: the slots ordinary work leaves idle fall back to pr-land lanes on parked, escalated or
unowned-red open PRs, decided by the shared rule ``omnimarket.handlers.rules_lab_fill_pr_land``. The
ledger replay adds what only it knows: a live lane's claim on a PR, and a landing lane's escalation.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from omnimarket.handlers.rules_lab_fill_pr_land import plan_pr_land_fallback, pr_key
from omnimarket.models.lab_fill import ModelLabFillPrLandFacts

from ..models import (
    EnumLabFillSkipReason,
    ModelLabFillCandidateInput,
    ModelLabFillDecision,
    ModelLabFillDeployment,
    ModelLabFillInputBaseline,
    ModelLabFillSelectionRequest,
    ModelLabFillSelectionResult,
)


def _stamp(value: str) -> datetime | None:
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at.astimezone(UTC) if at.tzinfo is not None else None


def _pr(value: str) -> str:
    return value.strip().rsplit("/", 1)[-1].lower()


def _pr_ids(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_.-]+#[0-9]+", value.lower()))


def _repo_of(pr: str) -> str:
    return pr.strip().rsplit("/", 1)[-1].partition("#")[0]


def _heads(
    values: tuple[str, ...], deployment: ModelLabFillDeployment
) -> tuple[str, ...]:
    return tuple(
        sorted(
            _pr(pr) + separator + sha
            for pr, separator, sha in (value.partition("@") for value in values)
            if _repo_of(_pr(pr)) not in deployment.companion_repositories
        )
    )


@dataclass(frozen=True, slots=True)
class _Row:
    ts: datetime
    kind: str
    lane: str
    prs: frozenset[str]
    subjects: frozenset[str]
    values: dict[str, str]


def _rows(lines: tuple[str, ...]) -> list[_Row]:
    rows = []
    for line in lines:
        cells = [cell.strip() for cell in line.split("|")]
        at = _stamp(cells[0])
        if len(cells) < 2 or at is None:
            continue
        values = dict(cell.split("=", 1) for cell in cells[2:] if "=" in cell)
        rows.append(
            _Row(
                at,
                cells[1],
                (values.get("lane") or values.get("from", "")).lower(),
                frozenset(
                    _pr_ids(values.get("pr", "") + " " + values.get("handed-off", ""))
                ),
                frozenset(
                    re.findall(
                        "OMN-[0-9]+",
                        " ".join(
                            values.get(key, "")
                            for key in ("ticket", "tickets", "related")
                        ),
                    )
                ),
                values,
            )
        )
    return sorted(rows, key=lambda row: row.ts)


@dataclass(slots=True)
class _Attempt:
    lane: str
    ts: datetime
    terminal: _Row | None = None


@dataclass(slots=True)
class _History:
    terminal_handoffs: dict[str, set[str]] = field(default_factory=dict)
    msg_handoffs: dict[str, set[str]] = field(default_factory=dict)
    handoffs: dict[str, datetime] = field(default_factory=dict)
    escalations: dict[str, datetime] = field(default_factory=dict)
    merged: set[str] = field(default_factory=set)
    open_prs: list[tuple[str, str, frozenset[str]]] = field(default_factory=list)
    attempts: dict[str, list[_Attempt]] = field(default_factory=dict)


def _replay(rows: list[_Row], landing_lane: str) -> _History:
    history = _History()
    unfinished: dict[
        str, list[_Attempt]
    ] = {}  # lab-fill attempts awaiting their TERMINAL, by lane
    latest_lanes: dict[str, str] = {}
    lane_prs: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        outcome = row.values.get("outcome", "").lower()
        history.merged.update(_pr_ids(row.values.get("merged", "")))
        if outcome == "merged" or row.values.get("result", "").lower() == "merged":
            history.merged.update(row.prs)
        recipients = {
            lane.strip().lower() for lane in row.values.get("to", "").split(",")
        }
        handoff = (row.kind == "MSG" and landing_lane in recipients) or (
            row.kind == "TERMINAL" and outcome == "handed-off"
        )
        handoff_prs = (
            row.prs
            if handoff
            else (
                _pr_ids(row.values.get("handed-off", ""))
                if row.kind == "STATUS"
                else set()
            )
        )
        for pr in handoff_prs:
            history.handoffs[pr] = row.ts
        if (
            row.kind == "MSG"
            and row.lane.startswith("landing")
            and landing_lane not in recipients
        ):
            for pr in row.prs:
                history.escalations[pr] = row.ts
        if not row.lane:
            continue
        if row.kind == "CLAIM":
            for ticket in row.subjects:
                latest_lanes[ticket] = row.lane
                history.terminal_handoffs.pop(ticket, None)
            history.open_prs.extend(
                (row.lane, pr, row.subjects) for pr in sorted(row.prs)
            )
            lab_lane = re.match(r"^lab-fill-omn([0-9]+)-", row.lane)
            if lab_lane and (ticket := "OMN-" + lab_lane[1]) in row.subjects:
                attempt = _Attempt(row.lane, row.ts)
                history.attempts.setdefault(ticket, []).append(attempt)
                unfinished.setdefault(row.lane, []).append(attempt)
        for ticket in row.subjects:
            if row.prs:
                lane_prs.setdefault((row.lane, ticket), set()).update(row.prs)
        if row.kind == "TERMINAL":
            history.open_prs = [
                claim
                for claim in history.open_prs
                if not (
                    claim[0] == row.lane
                    and (not row.subjects or not claim[2] or row.subjects & claim[2])
                )
            ]
            for attempt in unfinished.pop(row.lane, []):
                attempt.terminal = row
            for ticket in row.subjects:
                if latest_lanes.get(ticket, row.lane) != row.lane:
                    continue
                history.terminal_handoffs.pop(ticket, None)
                if outcome == "handed-off":
                    history.terminal_handoffs[ticket] = set(
                        row.prs or lane_prs.get((row.lane, ticket), set())
                    )
        if row.kind == "MSG" and landing_lane in recipients:
            for ticket in row.subjects:
                history.msg_handoffs.setdefault(ticket, set()).update(row.prs)
    return history


def repository_evidence(candidate: ModelLabFillCandidateInput) -> set[str]:
    """Repositories a candidate names by structure: its repo field, a label that is a repository
    slug, or a bracketed slug in its title. Free title words never count."""
    named = {candidate.repo} if candidate.repo else set()
    named.update(label for label in candidate.labels if "_" in label)
    named.update(re.findall(r"\[([A-Za-z0-9_.-]*_[A-Za-z0-9_.-]*)\]", candidate.title))
    return {_pr(name) for name in named}


def named_repositories(text: str, known: set[str]) -> set[str]:
    """Known repositories a text names as the first segment of a file path ("omnimarket/src/...",
    "src/repo_a/...", "$WORKSPACE/repo_b/plugins/...")."""
    if not known:
        return set()
    names = "|".join(re.escape(name) for name in sorted(known, key=len, reverse=True))
    found = re.findall(
        r"(?<![A-Za-z0-9_.-])(?:src/)?(" + names + r")/[A-Za-z0-9_.$]", text, re.I
    )
    return {name.lower() for name in found}


def work_repository(
    candidate: ModelLabFillCandidateInput,
    scope: set[str],
    deployment: ModelLabFillDeployment,
) -> tuple[str, str]:
    """The repository a candidate's lane works in, and the evidence for it (OMN-17427).

    A PR candidate works on its PR. Otherwise the PRs that name the ticket decide, newest first and
    knowledge-base PRs after the rest: a declared repository (an approved row's) is kept when one
    of them is in it, else the first one's repository wins. Without such a PR, the repositories
    its text names as file paths, by labels or by a bracketed slug decide when they name exactly
    one. The declared repository is the last resort, never a default for a ticket that has none."""
    declared = candidate.repo.strip()
    if candidate.pr:
        return declared or _repo_of(candidate.pr), "pr"
    prs = sorted(
        (
            _pr(pr)
            for pr in candidate.ticket_prs
            if "#" in pr
            and _repo_of(_pr(pr))
            not in (
                set(deployment.companion_repositories)
                | set(deployment.not_work_repositories)
            )
        ),
        key=lambda pr: _repo_of(pr) in deployment.document_repositories,
    )
    original = {_repo_of(pr).lower(): _repo_of(pr) for pr in candidate.ticket_prs}
    if declared and any(_repo_of(pr) == declared.lower() for pr in prs):
        return declared, "declared+ticket-pr"
    if prs:
        return original.get(_repo_of(prs[0]), _repo_of(prs[0])), "ticket-pr:" + prs[0]
    known = {name.lower() for name in scope} | (
        {declared.lower()} if declared else set()
    )
    named = named_repositories(candidate.title + " " + candidate.body, known)
    named |= repository_evidence(replace(candidate, repo=""))
    named -= set(deployment.companion_repositories) | set(
        deployment.not_work_repositories
    )
    if declared and declared.lower() in named:
        return declared, "declared+named-files"
    if len(named) == 1:
        return next(iter(named)), "named-files"
    return (declared, "declared") if declared else ("", "")


def _changed(
    baseline: ModelLabFillInputBaseline,
    candidate: ModelLabFillCandidateInput,
    deployment: ModelLabFillDeployment,
) -> list[str]:
    """What moved since the baseline: the ticket updated later than it read then, or a new head
    on one of its PRs. The repository's default branch moving is not a change to the ticket: on a
    busy repository it moved every tick and re-dispatched blocked tickets every 20 minutes
    (OMN-17427)."""
    changed = []
    before, after = (
        _stamp(baseline.ticket_updated_at),
        _stamp(candidate.ticket_updated_at),
    )
    # Later, not different: a candidate read from an older cache is not an update.
    if before is not None and after is not None and after > before:
        changed.append("ticket_updated_at")
    if (
        baseline.watcher_read
        and candidate.watcher_read
        and _heads(baseline.pr_heads, deployment)
        != _heads(candidate.pr_heads, deployment)
    ):
        changed.append("pr_heads")
    return changed


class HandlerLabFillSelection:
    """Pure selection: first matching gate wins, with one decision per candidate."""

    def handle(
        self, request: ModelLabFillSelectionRequest
    ) -> ModelLabFillSelectionResult:
        deployment = request.deployment
        now = _stamp(request.now)
        if now is None:
            raise ValueError("now must be an ISO timestamp with a timezone")
        history = _replay(_rows(request.ledger_lines), request.landing_lane.lower())
        history.merged.update(_pr(pr) for pr in request.closed_prs)
        held = {_pr(pr) for pr in request.controller.held_prs}
        controller_escalated = {_pr(pr) for pr in request.controller.escalated_prs}

        def escalated(pr: str) -> bool:
            handoff, escalation = history.handoffs.get(pr), history.escalations.get(pr)
            return pr in controller_escalated or (
                escalation is not None and (handoff is None or escalation > handoff)
            )

        baselines = []
        for baseline in request.baselines:
            attempts = history.attempts.get(baseline.ticket, [])
            recorded = _stamp(baseline.recorded_at)
            if (attempts and baseline.lane == attempts[-1].lane) or (
                not attempts
                and recorded is not None
                and now - timedelta(days=7) <= recorded <= now
            ):
                baselines.append(
                    replace(baseline, pr_heads=_heads(baseline.pr_heads, deployment))
                )
        decisions = []
        for candidate in request.candidates:
            decision, new_baseline = self._select(
                candidate, request, history, held, escalated, now, deployment
            )
            decisions.append(decision)
            if new_baseline is not None:
                baselines = [b for b in baselines if b.ticket != new_baseline.ticket]
                baselines.append(new_baseline)
        pr_land = None
        if request.pr_land is not None:
            ordinary = sum(d.reason is None for d in decisions)
            pr_land = plan_pr_land_fallback(
                self._with_ledger_facts(request, history, escalated),
                request.idle_slots - ordinary,
                request.now,
            )
        return ModelLabFillSelectionResult(
            tuple(decisions),
            tuple(sorted(baselines, key=lambda b: (b.ticket, b.lane))),
            pr_land,
        )

    @staticmethod
    def _with_ledger_facts(
        request: ModelLabFillSelectionRequest,
        history: _History,
        escalated: Callable[[str], bool],
    ) -> ModelLabFillPrLandFacts:
        """Fill each PR's owner from a live lane's claim, and its controller park and escalation."""
        facts = request.pr_land
        assert facts is not None
        landing = request.landing_lane.lower()
        claims: dict[str, str] = {}
        for lane, claimed, _ in history.open_prs:
            if lane != landing:
                claims.setdefault(claimed, lane)
        controller = request.controller
        parked = (
            {pr_key(p) for p in controller.parked_prs} if controller.read else set()
        )
        prs = []
        for pr in facts.prs:
            key = pr_key(pr.pr)
            prs.append(
                pr.model_copy(
                    update={
                        "owner": pr.owner or claims.get(key, ""),
                        "controller_parked": pr.controller_parked or key in parked,
                        "controller_escalated": pr.controller_escalated
                        or escalated(key),
                    }
                )
            )
        return facts.model_copy(update={"prs": tuple(prs)})

    @staticmethod
    def _select(
        candidate: ModelLabFillCandidateInput,
        request: ModelLabFillSelectionRequest,
        history: _History,
        held: set[str],
        escalated: Callable[[str], bool],
        now: datetime,
        deployment: ModelLabFillDeployment,
    ) -> tuple[ModelLabFillDecision, ModelLabFillInputBaseline | None]:
        pr = _pr(candidate.pr)
        repo, repo_source = work_repository(
            candidate, set(request.scope_repos), deployment
        )

        def decision(
            reason: EnumLabFillSkipReason | None = None,
            caller: str = "",
            detail: str = "",
        ) -> ModelLabFillDecision:
            return ModelLabFillDecision(
                candidate.key,
                candidate.ticket,
                pr,
                reason,
                caller,
                detail,
                repo,
                repo_source,
            )

        if candidate.kind == "defect" and request.scope_repos:
            # A defect is read team-wide by its pillar label or title term, so a side project's
            # ticket can match a different repository. Exclude it
            # on positive evidence only: unknown identity stays in scope.
            evidence = repository_evidence(candidate)
            in_project = (
                bool(request.project_id) and candidate.project_id == request.project_id
            )
            if (
                evidence
                and not in_project
                and not evidence & {_pr(r) for r in request.scope_repos}
            ):
                detail = ",".join(sorted(evidence))
                return decision(
                    EnumLabFillSkipReason.OUT_OF_SCOPE,
                    "out-of-repository-scope",
                    detail,
                ), None
        if candidate.kind not in ("pr-red", "pr-stalled"):
            pending = (
                history.terminal_handoffs.get(candidate.ticket, set())
                | history.msg_handoffs.get(candidate.ticket, set())
            ) - history.merged
            pending = {p for p in pending if not escalated(p)}
            if pending:
                detail = ",".join(sorted(pending))
                return decision(
                    EnumLabFillSkipReason.HANDED_OFF,
                    "owned:handed-off:" + detail,
                    detail,
                ), None
        elif (
            pr
            and pr in history.handoffs
            and pr not in history.merged
            and not escalated(pr)
        ):
            return decision(
                EnumLabFillSkipReason.HANDED_OFF, "owned:handed-off:" + pr, pr
            ), None
        elif (
            request.controller.read
            and pr in held
            and pr not in {_pr(p) for p in request.controller.escalated_prs}
        ):
            return decision(
                EnumLabFillSkipReason.HANDED_OFF,
                "owned:handed-off:" + pr,
                "controller-state",
            ), None
        if candidate.claim_holder:
            return decision(
                EnumLabFillSkipReason.OWNED, "owned", candidate.claim_holder
            ), None
        for lane, claimed_pr, _ in history.open_prs:
            if pr and pr == claimed_pr:
                return decision(EnumLabFillSkipReason.OWNED, "owned", lane), None
        if candidate.kind not in ("pr-red", "pr-stalled"):
            # Its implementation merged: whether the ticket is done is the closer's question
            # (dod-closeout-sweep reads every started ticket a merge names), not a code lane's.
            merged = sorted(
                {
                    p
                    for p in map(_pr, candidate.merged_prs)
                    if _repo_of(p) not in deployment.companion_repositories
                }
            )
            if merged:
                detail = ",".join(merged)
                return decision(
                    EnumLabFillSkipReason.IMPLEMENTATION_MERGED,
                    "implementation-merged:" + detail,
                    detail,
                ), None
        attempts = history.attempts.get(candidate.ticket, [])
        if not attempts:
            return decision(), None
        latest = attempts[-1]
        if latest.terminal is None:
            return decision(EnumLabFillSkipReason.OWNED, "owned", latest.lane), None
        outcome = latest.terminal.values.get("outcome", "").lower()
        if outcome in {"blocked", "partial", "no-op", "noop"}:
            reason, caller = (
                EnumLabFillSkipReason.UNCHANGED_INPUT,
                "unchanged-input:" + outcome,
            )
        elif (
            outcome != "handed-off"
            and sum(
                attempt.ts >= now - timedelta(hours=request.attempt_window_hours)
                for attempt in attempts
            )
            >= request.attempt_limit
        ):
            reason, caller = (
                EnumLabFillSkipReason.DISPATCH_LIMIT,
                "dispatch-limit:unchanged-input",
            )
        else:
            return decision(), None
        baseline = next(
            (
                b
                for b in request.baselines
                if b.ticket == candidate.ticket and b.lane == latest.lane
            ),
            None,
        )
        detail = outcome + ":" + latest.lane
        if baseline is not None:
            changed = _changed(baseline, candidate, deployment)
            if changed:
                return decision(detail="input-changed:" + ",".join(changed)), None
        if (
            reason is EnumLabFillSkipReason.UNCHANGED_INPUT
            and now - latest.terminal.ts >= timedelta(hours=request.cooldown_hours)
        ):
            return decision(detail="cooldown-elapsed:" + detail), None
        if baseline is not None:
            return decision(reason, caller, detail), None
        read_at = _stamp(candidate.facts_read_at)
        if read_at is None or read_at < latest.terminal.ts:
            return decision(reason, caller, detail + ":awaiting-fresh-read"), None
        return decision(reason, caller, detail), ModelLabFillInputBaseline(
            candidate.ticket,
            latest.lane,
            outcome,
            candidate.ticket_updated_at,
            _heads(candidate.pr_heads, deployment),
            candidate.main_sha,
            candidate.watcher_read,
            request.now,
        )
