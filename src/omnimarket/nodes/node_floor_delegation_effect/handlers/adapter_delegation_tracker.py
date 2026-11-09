# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure build of the delegation tracker (OMN-23081): readings in, the served document out.

Nothing here reads a file, runs a command or looks at a clock. A scenario passes only when its latest finished scheduled
run succeeded; a source that could not be read makes its rows ``unread`` and never a pass, a merge or a true start
condition.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from omnimarket.nodes.node_floor_delegation_effect.models.model_floor_delegation import (
    ModelDelegationCounts,
    ModelDelegationInputs,
    ModelDelegationLane,
    ModelDelegationRuntime,
    ModelDelegationTracker,
    ModelGroupCount,
    ModelMirrorTicket,
    ModelScenarioDecl,
    ModelScenarioRead,
    ModelScenarioResult,
    ModelStartPart,
    ModelTicketPr,
    ModelTicketResult,
    ModelWatcherPr,
    PrCi,
    TicketGroup,
)

GROUP_TITLES: dict[str, str] = {
    "done": "Done",
    "merged_awaiting_closer": "Merged, awaiting the closer",
    "pr_open": "Pull request open",
    "lane_running": "Lane running",
    "not_started": "Not started",
    "unread": "Not readable",
}
SHIPPABLE_RULE = (
    "Delegation is shippable when every scenario passes on its latest scheduled run "
    "(operator ruling 2026-11-08T14:44:15Z: it works when it works)."
)
_RELEASE_WORDS = re.compile(r"\b(released|deployed|pin)\b", re.IGNORECASE)
_TICKET_ID = re.compile(r"OMN-\d+")
_QUESTION_ID = re.compile(r"\bQ\d+\b")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def plan_ticket_ids(rt: ModelDelegationRuntime) -> tuple[str, ...]:
    return tuple(
        f"{rt.plan_ticket_prefix}{n}"
        for n in range(rt.plan_ticket_first, rt.plan_ticket_last + 1)
    )


def start_text(raw: str) -> str:
    """The start condition's words with the ticket links turned into plain ids."""
    return _MARKDOWN_LINK.sub(r"\1", raw).strip()


def referenced_tickets(raw: str) -> tuple[str, ...]:
    """Every ticket id a start condition names, once, in the order named."""
    seen: dict[str, None] = {}
    for found in _TICKET_ID.findall(start_text(raw)):
        seen.setdefault(found, None)
    return tuple(seen)


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _age_seconds(value: str, now: datetime) -> int | None:
    moment = _parse_iso(value)
    return None if moment is None else max(0, int((now - moment).total_seconds()))


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(seconds: int | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def _source_ok(inputs: ModelDelegationInputs, source_id: str) -> bool:
    return any(s.id == source_id and s.ok for s in inputs.sources)


_CI_SEVERITY: tuple[PrCi, ...] = ("red", "unread", "pending")


def _worst_ci(prs: list[ModelWatcherPr]) -> PrCi:
    cis = {p.ci for p in prs}
    for level in _CI_SEVERITY:
        if level in cis:
            return level
    return "green"


def _ticket_pr(p: ModelWatcherPr) -> ModelTicketPr:
    return ModelTicketPr(
        ref=p.ref, title=p.title, state=p.state, ci=p.ci, ci_note=p.ci_note, url=p.url
    )


def classify(
    ticket_id: str,
    mirror: ModelMirrorTicket | None,
    prs: list[ModelWatcherPr],
    lane: ModelDelegationLane | None,
    *,
    prs_readable: bool,
    lanes_readable: bool,
) -> tuple[TicketGroup, str]:
    """The one group a ticket is in, and the plain words that say why."""
    if mirror is not None and mirror.status_type == "completed":
        return "done", f"Closed in the ticket tracker ({mirror.linear_status})."
    open_prs = [p for p in prs if p.state == "open"]
    merged = [p for p in prs if p.state == "merged"]
    if open_prs:
        ci = _worst_ci(open_prs)
        refs = ", ".join(p.ref for p in open_prs)
        note = next((p.ci_note for p in open_prs if p.ci == "red" and p.ci_note), "")
        tail = f" {note}" if note else ""
        return "pr_open", f"{refs} open, checks {ci}.{tail}".strip()
    if merged:
        refs = ", ".join(p.ref for p in merged)
        return (
            "merged_awaiting_closer",
            f"{refs} merged; the ticket is not closed yet (the closer closes it on runtime proof).",
        )
    if not prs_readable:
        return (
            "unread",
            "The pull request watcher could not be read fresh, so this ticket's pull requests are unknown.",
        )
    if lane is not None:
        return (
            "lane_running",
            f"Lane {lane.lane} is working it on {lane.host or 'the launching host'}.",
        )
    if not lanes_readable:
        return (
            "unread",
            "The factory floor record could not be read fresh, so a lane on this ticket would not show.",
        )
    closed = [p for p in prs if p.state == "closed"]
    if closed:
        return (
            "not_started",
            f"No live lane and no open pull request ({', '.join(p.ref for p in closed)} closed without merging).",
        )
    return "not_started", "No pull request and no live lane."


def _is_met(
    ref: str,
    groups: dict[str, TicketGroup],
    mirror: dict[str, ModelMirrorTicket],
    prs_by_ticket: dict[str, list[ModelWatcherPr]],
) -> tuple[bool | None, str]:
    """Whether a ticket a start condition names is merged or done, and the words for it."""
    group = groups.get(ref)
    if group is not None:
        if group in ("done", "merged_awaiting_closer"):
            return True, f"{ref} is {GROUP_TITLES[group].lower()}"
        if group == "unread":
            return None, f"{ref} could not be read"
        return False, f"{ref} is not merged ({GROUP_TITLES[group].lower()})"
    m = mirror.get(ref)
    ticket_prs = prs_by_ticket.get(ref, [])
    if m is not None and m.status_type == "completed":
        return True, f"{ref} is done"
    if (
        ticket_prs
        and not any(p.state == "open" for p in ticket_prs)
        and any(p.state == "merged" for p in ticket_prs)
    ):
        return True, f"{ref} is merged"
    if m is None and not ticket_prs:
        return None, f"{ref} is outside the plan and the floor reads nothing about it"
    return False, f"{ref} is not done"


def evaluate_start(
    raw: str,
    groups: dict[str, TicketGroup],
    mirror: dict[str, ModelMirrorTicket],
    prs_by_ticket: dict[str, list[ModelWatcherPr]],
    answered: set[str],
) -> tuple[bool | None, str, tuple[ModelStartPart, ...], str]:
    """The start condition of a ticket: whether it holds, its words, each part, and a note on what is not read."""
    text = start_text(raw)
    if not text:
        return True, "No start condition is written on the ticket.", (), ""
    parts: list[ModelStartPart] = []
    for ref in referenced_tickets(raw):
        met, words = _is_met(ref, groups, mirror, prs_by_ticket)
        parts.append(ModelStartPart(text=words, met=met))
    seen_q: dict[str, None] = {}
    for q in _QUESTION_ID.findall(text):
        seen_q.setdefault(q, None)
    for q in seen_q:
        done = q in answered
        parts.append(
            ModelStartPart(
                text=f"the operator has {'answered' if done else 'not answered'} {q}",
                met=done,
            )
        )
    note = (
        "A release, a deploy or a version pin is part of this condition and is not read; only the merge is checked."
        if _RELEASE_WORDS.search(text)
        else ""
    )
    if not parts:
        return None, text, (), "The condition names nothing the floor can read."
    if any(p.met is False for p in parts):
        verdict: bool | None = False
    elif any(p.met is None for p in parts):
        verdict = None
    else:
        verdict = True
    return verdict, text, tuple(parts), note


def _scenario_result(
    decl: ModelScenarioDecl,
    read: ModelScenarioRead | None,
    reads_reason: str,
    builder_group: TicketGroup | None,
    rt: ModelDelegationRuntime,
    now: datetime,
) -> ModelScenarioResult:
    base = {
        "id": decl.id,
        "title": decl.title,
        "repo": decl.repo,
        "workflow": decl.workflow,
        "cadence_hours": decl.cadence_hours,
        "builder_ticket": decl.builder_ticket,
        "builder_group": builder_group,
        "workflow_url": f"{rt.github_url}/{decl.repo}/actions/workflows/{decl.workflow}",
        "read_seconds": read.read_seconds if read is not None else None,
    }
    if read is None:
        return ModelScenarioResult(
            **base,
            state="unread",
            result_text="Unread",
            reason=reads_reason or "The scenario runs were not read.",
        )
    read_age = _age_seconds(read.read_at, now)
    if read_age is None or read_age > rt.scenario_reads_max_age_seconds:
        return ModelScenarioResult(
            **base,
            state="unread",
            result_text="Unread",
            reason=(
                f"The workflow was last read {_ago(read_age)} ago, longer than the "
                f"{_ago(rt.scenario_reads_max_age_seconds)} limit, so its last result is not shown as current."
            ),
        )
    if read.read == "no_workflow":
        built = (
            f" Its builder, {decl.builder_ticket}, is {GROUP_TITLES[builder_group].lower()}."
            if decl.builder_ticket and builder_group
            else ""
        )
        return ModelScenarioResult(
            **base,
            state="no_workflow",
            result_text="Not built yet",
            reason=f"The repository has no workflow {decl.workflow}.{built}",
        )
    if read.read == "unread":
        return ModelScenarioResult(
            **base,
            state="unread",
            result_text="Unread",
            reason=read.reason or "The runs could not be read.",
        )
    ordered = sorted(read.runs, key=lambda r: r.created_at, reverse=True)
    finished = [r for r in ordered if r.status == "completed"]
    if not finished:
        return ModelScenarioResult(
            **base,
            state="no_run",
            result_text="No scheduled run has finished",
            reason=f"The workflow has no finished {rt.scenario_event} run.",
            in_progress=bool(ordered),
        )
    latest = finished[0]
    at = latest.updated_at or latest.created_at
    moment = _parse_iso(at)
    age = max(0, int((now - moment).total_seconds())) if moment else None
    passed = latest.conclusion == "success"
    newer_running = any(
        r.status != "completed" and r.created_at > latest.created_at for r in ordered
    )
    overdue = age is not None and age > 2 * decl.cadence_hours * 3600
    return ModelScenarioResult(
        **base,
        state="pass" if passed else "fail",
        result_text="Passed"
        if passed
        else f"Failed ({latest.conclusion or 'no conclusion'})",
        latest_at=at,
        latest_url=latest.url,
        latest_conclusion=latest.conclusion,
        age_seconds=age,
        overdue=overdue,
        in_progress=newer_running,
        recent=tuple("pass" if r.conclusion == "success" else "fail" for r in finished),
        reason=(
            f"Latest scheduled run finished {_ago(age)} ago"
            + (
                f", more than twice its {decl.cadence_hours} h cadence ago."
                if overdue
                else "."
            )
        ),
    )


def build_tracker(
    rt: ModelDelegationRuntime, inputs: ModelDelegationInputs, now: datetime
) -> ModelDelegationTracker:
    """The delegation tracker document from the readings."""
    ids = plan_ticket_ids(rt)
    mirror = {m.id: m for m in inputs.mirror}
    prs_by_ticket: dict[str, list[ModelWatcherPr]] = {}
    for pr in inputs.prs:
        prs_by_ticket.setdefault(pr.ticket, []).append(pr)
    live = {
        lane.ticket: lane
        for lane in inputs.lanes
        if not lane.stalled and lane.liveness not in rt.live_lane_dead_states
    }
    prs_readable = _source_ok(inputs, "watcher")
    lanes_readable = _source_ok(inputs, "record")
    groups: dict[str, TicketGroup] = {}
    why: dict[str, str] = {}
    for tid in ids:
        groups[tid], why[tid] = classify(
            tid,
            mirror.get(tid),
            prs_by_ticket.get(tid, []),
            live.get(tid),
            prs_readable=prs_readable,
            lanes_readable=lanes_readable,
        )
    answered = {a.id for a in rt.operator_answers}

    tickets: list[ModelTicketResult] = []
    for tid in ids:
        m = mirror.get(tid)
        group = groups[tid]
        prs = prs_by_ticket.get(tid, [])
        lane = live.get(tid)
        start_ok: bool | None = None
        start_words = ""
        parts: tuple[ModelStartPart, ...] = ()
        note = ""
        if group == "not_started":
            start_ok, start_words, parts, note = evaluate_start(
                m.start_when if m else "", groups, mirror, prs_by_ticket, answered
            )
            if m is None:
                start_ok, start_words, parts, note = (
                    None,
                    "",
                    (),
                    "The ticket is not in the ticket mirror, so its start condition is not read.",
                )
        open_prs = [p for p in prs if p.state == "open"]
        tickets.append(
            ModelTicketResult(
                id=tid,
                title=m.title if m else "(not in the ticket mirror)",
                plan_task=m.plan_task if m else "",
                group=group,
                linear_status=m.linear_status if m else "unread",
                mirror_read_at=m.read_at if m else "",
                url=f"{rt.ticket_url_prefix}{tid}",
                prs=tuple(_ticket_pr(p) for p in prs),
                pr_ci=_worst_ci(open_prs) if group == "pr_open" else None,
                lane=lane.lane if lane else "",
                host=(lane.host or "") if lane else "",
                start_condition=start_ok,
                start_text=start_words,
                start_parts=parts,
                start_note=note,
                why=why[tid],
            )
        )

    scenarios: list[ModelScenarioResult] = []
    reads = (
        {r.scenario: r for r in inputs.scenario_reads.reads}
        if inputs.scenario_reads
        else {}
    )
    reads_reason = (
        ""
        if inputs.scenario_reads
        else next(
            (s.detail for s in inputs.sources if s.id == "scenario_reads"),
            "The scenario runs were not read.",
        )
    )
    for decl in rt.scenarios:
        builder_group = groups.get(decl.builder_ticket) if decl.builder_ticket else None
        scenarios.append(
            _scenario_result(
                decl, reads.get(decl.id), reads_reason, builder_group, rt, now
            )
        )

    passing = [s for s in scenarios if s.state == "pass"]
    shippable = len(passing) == len(scenarios) and len(scenarios) > 0
    blocking = tuple(s.id for s in scenarios if s.state != "pass")
    if shippable:
        line = f"Delegation is shippable: all {len(scenarios)} scenarios pass on their latest scheduled run."
    else:
        line = (
            f"Delegation is not shippable: {len(passing)} of {len(scenarios)} scenarios "
            f"pass on their latest scheduled run."
        )

    group_counts = tuple(
        ModelGroupCount(
            id=gid,
            title=GROUP_TITLES[gid],
            count=sum(1 for t in tickets if t.group == gid),
            ticket_ids=tuple(t.id for t in tickets if t.group == gid),
        )
        for gid in GROUP_TITLES
    )
    pr_open = [t for t in tickets if t.group == "pr_open"]
    not_started = [t for t in tickets if t.group == "not_started"]
    counts = ModelDelegationCounts(
        scenarios_total=len(scenarios),
        scenarios_passing=len(passing),
        scenarios_failing=sum(1 for s in scenarios if s.state == "fail"),
        scenarios_not_built=sum(1 for s in scenarios if s.state == "no_workflow"),
        scenarios_no_run=sum(1 for s in scenarios if s.state == "no_run"),
        scenarios_unread=sum(1 for s in scenarios if s.state == "unread"),
        tickets_total=len(tickets),
        groups=group_counts,
        pr_open_red=sum(1 for t in pr_open if t.pr_ci == "red"),
        pr_open_green=sum(1 for t in pr_open if t.pr_ci == "green"),
        pr_open_pending=sum(1 for t in pr_open if t.pr_ci == "pending"),
        pr_open_unread=sum(1 for t in pr_open if t.pr_ci == "unread"),
        start_true=sum(1 for t in not_started if t.start_condition is True),
        start_false=sum(1 for t in not_started if t.start_condition is False),
        start_unread=sum(1 for t in not_started if t.start_condition is None),
    )
    return ModelDelegationTracker(
        generated_at=_iso(now),
        shippable=shippable,
        shippable_line=line,
        shippable_rule=SHIPPABLE_RULE,
        blocking=blocking,
        counts=counts,
        scenarios=tuple(scenarios),
        tickets=tuple(tickets),
        sources=inputs.sources,
        details_page_size=rt.details_page_size,
    )
