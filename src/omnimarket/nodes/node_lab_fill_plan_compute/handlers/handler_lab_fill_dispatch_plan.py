# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Choose which lab-fill work becomes a lane, and on which host (OMN-20668).

The one place work is chosen. Skips every owned, fenced, excluded or out-of-scope
candidate; keeps one candidate per PR and per ticket (the more urgent kind wins);
orders tier 1 by kind and ticket and the approved tier by declared value; dispatches
at most the run's budget; counts a slot on the host with the most lanes left that
holds the lane's engine. A lane is pinned only when its work names a host.

OMN-20864: a pr-land item (the idle-slot fallback lab-fill selection plans on a parked, escalated or
unowned-red PR) ranks after every other kind, so it takes only slots ordinary work leaves idle. It keeps
the selection's order, takes the run's parent ticket when its PR names none, is deduplicated by PR only,
and its lane is named for the PR.

OMN-17427: a dod-verify item (a ticket lab-fill selection skipped because its implementation merged,
taken for an idle slot) ranks after pr-land, is scoped by the kind it was enumerated as
(``origin_kind``), carries no PR of its own, and its lane is ``lab-fill-verify-<ticket>-<tag>``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cmp_to_key

from omnimarket.handlers.rules_lab_fill_closer import closer_route
from omnimarket.models.lab_fill.model_lab_fill_closer import ModelLabFillCloserRoute

from ..models import (
    ModelLabFillDispatchItem,
    ModelLabFillDispatchPlanRequest,
    ModelLabFillDispatchPlanResult,
    ModelLabFillFallback,
    ModelLabFillHostCapacity,
    ModelLabFillMachineryPolicy,
    ModelLabFillPlanConfig,
    ModelLabFillSkipEntry,
)
from .helpers_js_value import get, has_word, has_word_stem, text_of, truthy, words
from .helpers_machinery_policy import machinery_policy

APPROVED_KINDS = ("process-fix", "partial-node", "wiring")
PR_LAND = "pr-land"
DOD_VERIFY = "dod-verify"
MACHINERY = "machinery"
PR_KINDS = ("pr-red", "pr-stalled", PR_LAND)
KINDS = ("pr-red", "pr-stalled", "defect", "ticket", *APPROVED_KINDS)
# The tier-1 source kinds (plan-stalled, ruling-pending, lane-resend) are scoped by their source, never by
# the sprint, and an umbrella ticket fences them like a ticket (OMN-20780).
SOURCE_KINDS = ("plan-stalled", "ruling-pending", "lane-resend")
# The kinds a plan dispatches. pr-land is not an enumerated source kind (the STATUS row's per-kind cells
# iterate KINDS): lab-fill selection's idle-slot fallback produces it (OMN-20864). machinery is the
# factory's own breaks (OMN-22413): the fill effect hands the plan the facts, the machinery handler
# classifies them, and the policy's tier order in contract.yaml ranks the kind.
DISPATCH_KINDS = (*KINDS, *SOURCE_KINDS, PR_LAND, DOD_VERIFY, MACHINERY)
LANE = "lab-fill"
# The dispatched lane's ROUTE line. The runner refuses a --model or --effort that
# differs from it, so the two are rendered from one table.
LANE_ROUTES = {
    "sonnet": "ROUTE: band=B3 score=10 dims=S2A2R2V2N0C1J1 dest=sonnet-lane model=sonnet effort=high",
    "codex": "ROUTE: band=B2 score=7 dims=S1A1R1V2N1C1J0 dest=codex-draft+sonnet-lane model=codex effort=high",
}
_TICKET_RE = re.compile(r"OMN-[0-9]+")
_PR_RE = re.compile(r"([A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+#[0-9]+")
# A PR value is <repo>#<n> or <owner>/<repo>#<n>, with an optional :<cause> suffix the landing controller adds.
_PR_ITEM_RE = re.compile(
    r"((?:[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+#[0-9]+)(?::([A-Za-z0-9][A-Za-z0-9_.:-]*))?"
)
_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_RUN_TAG_RE = re.compile(r"[0-9]{4}-([0-9]{2})-([0-9]{2})(?:T([0-9]{2})([0-9]{2})Z)?")
_MAX_SAFE_INTEGER = 2**53 - 1


def parse_pr_item(value: object) -> tuple[str, str] | None:
    """(pr, cause) for a well-formed PR value, None when no #<n> stands in it."""
    match = _PR_ITEM_RE.fullmatch(value.strip() if isinstance(value, str) else "")
    return (match.group(1), match.group(2) or "") if match else None


def _pr_key(candidate: object) -> str:
    parsed = parse_pr_item(get(candidate, "pr"))
    if parsed is None:
        return ""
    pr = parsed[0]
    return (pr.rsplit("/", 1)[-1] if "/" in pr else pr).lower()


def normalize_pr(candidate: dict[str, object]) -> dict[str, object]:
    """The candidate with its PR value reduced to <repo>#<n>, the suffix carried as cause."""
    parsed = parse_pr_item(candidate.get("pr"))
    if parsed is None:
        return candidate
    pr, cause = parsed
    carried = candidate.get("cause") or cause
    return {**candidate, "pr": pr, **({"cause": carried} if carried else {})}


def priority_class(candidate: dict[str, object], cfg: ModelLabFillPlanConfig) -> str:
    """The lane priority class, from the run's declared classes (RULING 2026-10-15T08:14:02Z)."""
    mvp, process, other = cfg.priority_classes
    repo = text_of(candidate.get("repo")).strip().lower()
    kind = candidate.get("kind")
    is_pr = kind in ("pr-red", "pr-stalled")
    delegation = any(
        label in cfg.defect_labels for label in words(candidate.get("labels"))
    ) or any(has_word_stem(candidate.get("title"), t) for t in cfg.defect_terms)
    if is_pr and (repo in cfg.mvp_repos or delegation):
        return mvp
    if kind in ("process-fix", "ruling-pending") or (
        is_pr and repo in cfg.process_repos
    ):
        return process
    return other


def _ticket_number(ticket: object) -> int:
    match = re.fullmatch(r"OMN-([0-9]+)", text_of(ticket))
    return int(match.group(1)) if match else _MAX_SAFE_INTEGER


def _run_tag(run_key: str) -> str:
    match = _RUN_TAG_RE.fullmatch(run_key)
    if not match:
        return "manual"
    return (
        f"{match.group(1)}{match.group(2)}{match.group(3) or ''}{match.group(4) or ''}"
    )


def _lane_name(candidate: object, tag: str) -> str:
    if get(candidate, "kind") == PR_LAND:
        repo, _, number = _pr_key(candidate).partition("#")
        return f"{LANE}-land-{re.sub(r'[^a-z0-9_-]', '', repo)}-{number}-{tag}"
    if get(candidate, "kind") == MACHINERY:
        digest = hashlib.sha1(
            text_of(get(candidate, "machinery_key")).encode(), usedforsecurity=False
        ).hexdigest()[:8]
        return f"{LANE}-machinery-{digest}-{tag}"
    ticket = re.sub(r"[^a-z0-9]", "", text_of(get(candidate, "ticket")).lower())
    if get(candidate, "kind") == DOD_VERIFY:
        return f"{LANE}-verify-{ticket}-{tag}"
    return f"{LANE}-{ticket}-{tag}"


def dod_verify_item(candidate: dict[str, object], merged: str) -> dict[str, object]:
    """The enumerated candidate as DoD-verify work on the PRs its implementation merged in."""
    return {
        **{k: v for k, v in candidate.items() if k not in ("pr", "head_ref", "host")},
        "kind": DOD_VERIFY,
        "origin_kind": candidate.get("kind"),
        "self_contained_code": False,
        "title": f"implementation merged in {merged}: {text_of(candidate.get('title'))}",
    }


def skip_reason(
    candidate: object, cfg: ModelLabFillPlanConfig, fenced: set[str]
) -> str | None:
    """None when the candidate may be dispatched, else why not."""
    if not isinstance(candidate, dict):
        return "malformed"
    c = candidate
    kind = c.get("kind")
    if kind not in DISPATCH_KINDS:
        return "unknown-kind"
    if kind == DOD_VERIFY:
        # Scoped, fenced and owned as the kind it was enumerated as.
        kind = c.get("origin_kind")
        if kind not in KINDS:
            return "unknown-kind"
    ticket_raw = c.get("ticket")
    ticket = ticket_raw.strip() if isinstance(ticket_raw, str) else ""
    if _TICKET_RE.fullmatch(ticket) is None:
        return "no-ticket"
    assignee = c.get("assignee_id")
    if truthy(assignee) and (not cfg.operator_id or assignee != cfg.operator_id):
        return "assigned-other"
    if truthy(c.get("hold_reason")):
        return f"held:{text_of(c['hold_reason'])}"
    approved = kind in APPROVED_KINDS
    if approved and (
        _NAME_RE.fullmatch(text_of(c.get("id"))) is None
        or _NAME_RE.fullmatch(text_of(c.get("repo"))) is None
    ):
        return "malformed-approved"
    if approved and truthy(c.get("done")):
        return "done"
    if approved and truthy(c.get("blocked_until")):
        return f"blocked-until:{text_of(c['blocked_until'])}"
    is_pr = kind in PR_KINDS
    if (
        is_pr
        and _PR_RE.fullmatch(text_of(c.get("pr"))) is None
        and parse_pr_item(c.get("pr")) is None
    ):
        return "no-pr"
    # A PR value with no #<n> in it would reach the runner's --pr and exit 2 on every tick.
    pr_value = c.get("pr")
    if (
        isinstance(pr_value, str)
        and pr_value.strip()
        and parse_pr_item(pr_value) is None
    ):
        return "pr-ref-no-number"
    if (
        kind in ("ticket", "defect") or kind in SOURCE_KINDS
    ) and ticket == cfg.parent_ticket:
        return "umbrella"
    labels = c.get("labels")
    parts = [
        c.get("title"),
        *(labels if isinstance(labels, list) else []),
        c.get("area"),
    ]
    text = " ".join(
        "" if p is None else text_of(p) if not isinstance(p, str) else p for p in parts
    )
    for area in cfg.exclude_areas:
        if has_word(text, area):
            return f"excluded-area:{area}"
    milestone = text_of(c.get("milestone")).strip().lower()
    in_milestone = milestone == "" or milestone == cfg.milestone.lower()
    if kind in (PR_LAND, MACHINERY):
        scoped = not cfg.pr_repos or text_of(c.get("repo")).lower() in cfg.pr_repos
    elif cfg.project_id:
        scoped = (
            text_of(c.get("repo")).lower() in cfg.pr_repos
            if is_pr
            else c.get("project_id") == cfg.project_id
        )
    else:
        scoped = truthy(c.get("under_parent")) and in_milestone
    if kind == "defect":
        is_defect = any(label in cfg.defect_labels for label in words(labels)) or any(
            has_word_stem(c.get("title"), term) for term in cfg.defect_terms
        )
        if not is_defect:
            return "not-a-pillar-defect"
    elif not approved and kind != MACHINERY and kind not in SOURCE_KINDS and not scoped:
        return "out-of-scope"
    holder = c.get("claim_holder")
    holder = holder.strip() if isinstance(holder, str) else ""
    if holder:
        return f"owned:{holder}"
    pr_claim = c.get("pr_claim")
    pr_claim = pr_claim.strip() if isinstance(pr_claim, str) else ""
    if pr_claim:
        return f"owned:{pr_claim}"
    if ticket != cfg.parent_ticket and ticket in fenced:
        return "fenced"
    if is_pr and _NAME_RE.fullmatch(text_of(c.get("repo"))) is None:
        return "no-repo"
    return None


@dataclass
class _Slot:
    host: ModelLabFillHostCapacity
    left: int


def _entry(
    candidate: object, cfg: ModelLabFillPlanConfig, reason: str, host: str | None = None
) -> ModelLabFillSkipEntry:
    ident = get(candidate, "pr") or get(candidate, "ticket")
    fields: dict[str, object] = {
        "id": text_of(ident) if candidate is not None and ident else "?",
        "reason": reason,
    }
    if cfg.project_id and (
        candidate is None or (isinstance(candidate, dict) and "kind" in candidate)
    ):
        fields["kind"] = get(candidate, "kind")
    if host is not None:
        fields["host"] = host
    return ModelLabFillSkipEntry(**fields)


def kind_priority(policy: ModelLabFillMachineryPolicy) -> dict[str, int]:
    """Rank of every dispatch kind, lower first, from the policy's tier order.

    A red PR blocks a merge now, a stalled one blocks it soon, a defect degrades every lane, a
    ticket is new work: the contract's order says where machinery sits among them.
    """
    rank = {k: i for i, group in enumerate(policy.tier_order) for k in group}
    missing = [k for k in DISPATCH_KINDS if k not in rank]
    if missing:
        raise ValueError(f"tier_order does not rank {missing}")
    return rank


def lane_class(candidate: dict[str, object], cfg: ModelLabFillPlanConfig) -> str:
    """The class a lane is ordered and dispatched by; machinery states its own."""
    if candidate.get("kind") == MACHINERY:
        return text_of(candidate.get("priority_class")) or cfg.priority_classes[-1]
    return priority_class(candidate, cfg)


def _class_rank(name: str, cfg: ModelLabFillPlanConfig) -> int:
    classes = cfg.priority_classes
    return classes.index(name) if name in classes else -1


def _machinery_segment(
    candidate: dict[str, object], priority: Mapping[str, int]
) -> int:
    """0 ranks before machinery by tier, 1 with it (machinery included), 2 after it."""
    delta = priority[str(candidate["kind"])] - priority[MACHINERY]
    return 0 if delta < 0 else 1 if delta == 0 else 2


def _compare(
    x: dict[str, object],
    y: dict[str, object],
    priority: Mapping[str, int],
    cfg: ModelLabFillPlanConfig,
) -> int:
    # Open-count gated work ranks after all ungated work; then the lane priority class (the run's declared order,
    # first ranked first); then a PR the landing controller parked or escalated back (OMN-20780).
    by_gate = int(truthy(x.get("gated"))) - int(truthy(y.get("gated")))
    if by_gate:
        return by_gate
    # Machinery is ranked against every kind by the contract's tier order alone (OMN-22413). Each lane
    # sits before, with, or after machinery's own rank, and that segment is the next sort key, so the
    # class and landing ranks only order lanes inside one segment and the order stays transitive under
    # any tier_order. Machinery leads the lanes that share its rank.
    by_segment = _machinery_segment(x, priority) - _machinery_segment(y, priority)
    if by_segment:
        return by_segment
    if MACHINERY in (x["kind"], y["kind"]):
        return int(y["kind"] == MACHINERY) - int(x["kind"] == MACHINERY)
    by_class = _class_rank(lane_class(x, cfg), cfg) - _class_rank(
        lane_class(y, cfg), cfg
    )
    if by_class:
        return by_class
    by_landing = int(not truthy(x.get("landing"))) - int(not truthy(y.get("landing")))
    if by_landing:
        return by_landing
    by_kind = priority[str(x["kind"])] - priority[str(y["kind"])]
    if by_kind:
        return by_kind
    if x["kind"] in APPROVED_KINDS and y["kind"] in APPROVED_KINDS:
        return 0
    if x["kind"] == y["kind"] and x["kind"] in (PR_LAND, DOD_VERIFY, MACHINERY):
        return 0
    by_ticket = _ticket_number(x["ticket"]) - _ticket_number(y["ticket"])
    if by_ticket:
        return by_ticket
    kx, ky = _pr_key(x), _pr_key(y)
    return -1 if kx < ky else 1 if kx > ky else 0


class HandlerLabFillDispatchPlan:
    """Select lanes from the candidates the enumerate read returned."""

    def handle(
        self, request: ModelLabFillDispatchPlanRequest
    ) -> ModelLabFillDispatchPlanResult:
        cfg = request.config
        fenced = set(request.fenced)
        skipped: list[ModelLabFillSkipEntry] = []
        routed: list[ModelLabFillCloserRoute] = []
        ok: list[dict[str, object]] = []
        for candidate in request.candidates:
            if (
                isinstance(candidate, dict)
                and candidate.get("kind") in (PR_LAND, MACHINERY)
                and not text_of(candidate.get("ticket")).strip()
            ):
                candidate = {**candidate, "ticket": cfg.parent_ticket}
            why = skip_reason(candidate, cfg, fenced)
            if why:
                skipped.append(_entry(candidate, cfg, why))
            elif isinstance(candidate, dict):
                # OMN-23376: work a merged change or a lane's own report already carries is the closer's, not a lane's.
                ticket = text_of(candidate.get("ticket"))
                route = closer_route(
                    ticket,
                    text_of(candidate.get("kind")),
                    merged_prs=request.merged_prs.get(ticket, ()),
                    built=request.built_reports,
                    exempt_kinds=request.closer_exempt_kinds,
                    closer=request.closer,
                )
                if route is None:
                    ok.append(normalize_pr(candidate))
                elif ticket not in {r.ticket for r in routed}:
                    routed.append(route)
                    skipped.append(_entry(candidate, cfg, route.reason.value))
                else:
                    skipped.append(_entry(candidate, cfg, "duplicate"))
        # A stable sort keeps the approved file's value order inside the approved tier.
        priority = kind_priority(request.machinery or machinery_policy())
        ok.sort(key=cmp_to_key(lambda x, y: _compare(x, y, priority, cfg)))
        seen_pr: set[str] = set()
        seen_ticket: set[object] = set()
        seen_machinery: set[str] = set()
        unique: list[dict[str, object]] = []
        for c in ok:
            key = _pr_key(c)
            # A pr-land item and a friction or tick machinery class share a ticket with other work
            # (the run's parent, or an umbrella): they are deduplicated by PR and by class key.
            by_ticket = c["kind"] != PR_LAND and (
                c["kind"] != MACHINERY or c.get("source") == "machinery-ticket"
            )
            class_key = (
                text_of(c.get("machinery_key")) if c["kind"] == MACHINERY else ""
            )
            if (
                (key and key in seen_pr)
                or (by_ticket and c["ticket"] in seen_ticket)
                or (class_key and class_key in seen_machinery)
            ):
                skipped.append(_entry(c, cfg, "duplicate"))
                continue
            if key:
                seen_pr.add(key)
            if by_ticket:
                seen_ticket.add(c["ticket"])
            if class_key:
                seen_machinery.add(class_key)
            unique.append(c)
        slots: dict[str, _Slot] = {}
        for host in request.capacity.hosts:
            if host.lanes > 0:
                slots[host.name] = _Slot(host, host.lanes)
        budget = max(0, min(cfg.max_lanes, request.capacity.budget))
        tag = _run_tag(cfg.run_key)
        dispatch: list[ModelLabFillDispatchItem] = []
        deferred: list[ModelLabFillSkipEntry] = []
        gated_over: dict[str, int] = {}

        def best(engine: str, pin: str) -> _Slot | None:
            fit = [
                s
                for s in slots.values()
                if (not pin or s.host.name == pin)
                and s.left > 0
                and (s.host.codex if engine == "codex" else s.host.claude)
            ]
            fit.sort(
                key=lambda s: (
                    -s.left,
                    s.host.load_per_core if s.host.load_per_core is not None else 9,
                    s.host.name,
                )
            )
            return fit[0] if fit else None

        for c in unique:
            if len(dispatch) >= budget:
                if truthy(c.get("gated")):
                    kind_key = text_of(c["kind"])
                    gated_over[kind_key] = gated_over.get(kind_key, 0) + 1
                else:
                    deferred.append(_entry(c, cfg, "over-cap"))
                continue
            host_value = c.get("host")
            pin = host_value.strip() if isinstance(host_value, str) else ""
            # A Codex lane opens a new PR and cannot push to an existing PR branch, so an
            # item with a pr stays on sonnet and defers when no claude host has room.
            engine = (
                "codex"
                if truthy(c.get("self_contained_code")) and not truthy(c.get("pr"))
                else "sonnet"
            )
            slot = best(engine, pin)
            fallback: ModelLabFillFallback | None = None
            if slot is None and engine == "codex":
                engine = "sonnet"
                slot = best(engine, pin)
                if slot is not None:
                    fallback = ModelLabFillFallback.model_validate(
                        {"from": "codex", "reason": "no-codex-host"}
                    )
            elif slot is None and engine == "sonnet" and not truthy(c.get("pr")):
                engine = "codex"
                slot = best(engine, pin)
            if slot is None:
                deferred.append(
                    _entry(
                        c,
                        cfg,
                        "pinned-host-no-headroom" if pin else "no-host-for-engine",
                        pin or None,
                    )
                )
                continue
            slot.left -= 1
            fields: dict[str, object] = {
                "lane": _lane_name(c, tag),
                "kind": c["kind"],
                "ticket": c["ticket"],
                "pr": text_of(c.get("pr")),
                "repo": text_of(c.get("repo")),
                "ref": text_of(c.get("head_ref")),
                "title": (
                    f"{text_of(c.get('title'))}: {text_of(c.get('evidence'))}"
                    if c["kind"] == MACHINERY
                    else text_of(c.get("title"))
                )[:160],
                "updated_at": text_of(c.get("updated_at")),
                "host": slot.host.name,
                "pinned": bool(pin),
                "engine": engine,
                "route": LANE_ROUTES[engine],
            }
            if truthy(c.get("gated")):
                fields["gated"] = True
            if c["kind"] in APPROVED_KINDS:
                fields["id"] = c.get("id")
            if truthy(c.get("cause")):
                fields["cause"] = text_of(c["cause"])
            if c.get("escalated") is True:
                fields["escalated"] = True
            if c["kind"] in SOURCE_KINDS:
                fields["evidence"] = text_of(c.get("evidence"))[:240]
            if truthy(c.get("fix_evidence")):
                fields["fix_evidence"] = text_of(c["fix_evidence"])[:200]
            fields["priority_class"] = lane_class(c, cfg) or None
            if c["kind"] == MACHINERY:
                fields["machinery_key"] = text_of(c.get("machinery_key"))
            if truthy(c.get("repo_source")):
                fields["repo_source"] = c["repo_source"]
            if fallback is not None:
                fields["fallback"] = fallback
            dispatch.append(ModelLabFillDispatchItem(**fields))
        for gated_kind, count in gated_over.items():
            deferred.append(
                ModelLabFillSkipEntry(
                    id=f"open-count-gate:{gated_kind}",
                    reason="open-count-gate:rising",
                    n=count,
                    **({"kind": gated_kind} if cfg.project_id else {}),
                )
            )
        return ModelLabFillDispatchPlanResult(
            dispatch=tuple(dispatch),
            skipped=tuple(skipped),
            deferred=tuple(deferred),
            budget=budget,
            # Set only when there is a route, so a plan with none answers the keys the workflow reads today.
            **({"closer": tuple(routed)} if routed else {}),
        )
