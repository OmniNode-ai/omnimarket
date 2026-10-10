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
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cmp_to_key

from ..models import (
    ModelLabFillDispatchItem,
    ModelLabFillDispatchPlanRequest,
    ModelLabFillDispatchPlanResult,
    ModelLabFillFallback,
    ModelLabFillHostCapacity,
    ModelLabFillPlanConfig,
    ModelLabFillSkipEntry,
)
from .helpers_js_value import get, has_word, has_word_stem, text_of, truthy, words

APPROVED_KINDS = ("process-fix", "partial-node", "wiring")
PR_LAND = "pr-land"
PR_KINDS = ("pr-red", "pr-stalled", PR_LAND)
KINDS = ("pr-red", "pr-stalled", "defect", "ticket", *APPROVED_KINDS)
# The kinds a plan dispatches. pr-land is not an enumerated source kind (the STATUS row's per-kind cells
# iterate KINDS): lab-fill selection's idle-slot fallback produces it (OMN-20864).
DISPATCH_KINDS = (*KINDS, PR_LAND)
# Lower first: a red PR blocks a merge now, a stalled one blocks it soon, a defect
# degrades every lane, a ticket is new work.
KIND_PRIORITY = {
    "pr-red": 0,
    "pr-stalled": 1,
    "defect": 2,
    "ticket": 3,
    "process-fix": 4,
    "partial-node": 4,
    "wiring": 4,
    PR_LAND: 5,
}
LANE = "lab-fill"
# The dispatched lane's ROUTE line. The runner refuses a --model or --effort that
# differs from it, so the two are rendered from one table.
LANE_ROUTES = {
    "sonnet": "ROUTE: band=B3 score=10 dims=S2A2R2V2N0C1J1 dest=sonnet-lane model=sonnet effort=high",
    "codex": "ROUTE: band=B2 score=7 dims=S1A1R1V2N1C1J0 dest=codex-draft+sonnet-lane model=codex effort=high",
}
_TICKET_RE = re.compile(r"OMN-[0-9]+")
_PR_RE = re.compile(r"([A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+#[0-9]+")
_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_RUN_TAG_RE = re.compile(r"[0-9]{4}-([0-9]{2})-([0-9]{2})(?:T([0-9]{2})([0-9]{2})Z)?")
_MAX_SAFE_INTEGER = 2**53 - 1


def _pr_key(candidate: object) -> str:
    pr = get(candidate, "pr")
    pr = pr.strip() if isinstance(pr, str) else ""
    if not pr:
        return ""
    return (pr.rsplit("/", 1)[-1] if "/" in pr else pr).lower()


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
    ticket = re.sub(r"[^a-z0-9]", "", text_of(get(candidate, "ticket")).lower())
    return f"{LANE}-{ticket}-{tag}"


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
    if is_pr and _PR_RE.fullmatch(text_of(c.get("pr"))) is None:
        return "no-pr"
    if kind in ("ticket", "defect") and ticket == cfg.parent_ticket:
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
    if kind == PR_LAND:
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
    elif not approved and not scoped:
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


def _compare(x: dict[str, object], y: dict[str, object]) -> int:
    by_kind = KIND_PRIORITY[str(x["kind"])] - KIND_PRIORITY[str(y["kind"])]
    if by_kind:
        return by_kind
    if x["kind"] in APPROVED_KINDS and y["kind"] in APPROVED_KINDS:
        return 0
    if x["kind"] == PR_LAND and y["kind"] == PR_LAND:
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
        ok: list[dict[str, object]] = []
        for candidate in request.candidates:
            if (
                isinstance(candidate, dict)
                and candidate.get("kind") == PR_LAND
                and not text_of(candidate.get("ticket")).strip()
            ):
                candidate = {**candidate, "ticket": cfg.parent_ticket}
            why = skip_reason(candidate, cfg, fenced)
            if why:
                skipped.append(_entry(candidate, cfg, why))
            elif isinstance(candidate, dict):
                ok.append(candidate)
        # A stable sort keeps the approved file's value order inside the approved tier.
        ok.sort(key=cmp_to_key(_compare))
        seen_pr: set[str] = set()
        seen_ticket: set[object] = set()
        unique: list[dict[str, object]] = []
        for c in ok:
            key = _pr_key(c)
            by_ticket = c["kind"] != PR_LAND
            if (key and key in seen_pr) or (by_ticket and c["ticket"] in seen_ticket):
                skipped.append(_entry(c, cfg, "duplicate"))
                continue
            if key:
                seen_pr.add(key)
            if by_ticket:
                seen_ticket.add(c["ticket"])
            unique.append(c)
        slots: dict[str, _Slot] = {}
        for host in request.capacity.hosts:
            if host.lanes > 0:
                slots[host.name] = _Slot(host, host.lanes)
        budget = max(0, min(cfg.max_lanes, request.capacity.budget))
        tag = _run_tag(cfg.run_key)
        dispatch: list[ModelLabFillDispatchItem] = []
        deferred: list[ModelLabFillSkipEntry] = []

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
                "title": text_of(c.get("title"))[:160],
                "updated_at": text_of(c.get("updated_at")),
                "host": slot.host.name,
                "pinned": bool(pin),
                "engine": engine,
                "route": LANE_ROUTES[engine],
            }
            if c["kind"] in APPROVED_KINDS:
                fields["id"] = c.get("id")
            if truthy(c.get("repo_source")):
                fields["repo_source"] = c["repo_source"]
            if fallback is not None:
                fields["fallback"] = fallback
            dispatch.append(ModelLabFillDispatchItem(**fields))
        return ModelLabFillDispatchPlanResult(
            dispatch=tuple(dispatch),
            skipped=tuple(skipped),
            deferred=tuple(deferred),
            budget=budget,
        )
