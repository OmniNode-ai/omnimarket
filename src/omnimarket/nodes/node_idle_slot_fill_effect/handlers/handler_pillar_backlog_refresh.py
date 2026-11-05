# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rebuild stored work from the tick's live sources under the fill's existing lock.

An unread source leaves the file intact and offers no stored work this tick. A
read-only tick computes the same projection without writing either state file.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.handler_lab_fill_live_tickets import LabFillLiveReadError, Post
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.handlers.scm.handler_scm_local_state import default_claims_dir
from omnimarket.nodes.node_idle_slot_fill_effect.models.model_idle_slot_fill_overlay import (
    ModelIdleSlotFillOverlay,
)
from omnimarket.nodes.node_idle_slot_fill_effect.models.model_pillar_backlog_refresh import (
    ModelPillarBacklogPolicy,
    ModelPillarBacklogRefreshRequest,
    ModelPillarBacklogRefreshResult,
)

from .handler_idle_slot_fill import (
    _PR_REF,
    LOCK_FILE,
    SENT_FILE,
    _closed,
    _fact_newest,
    _issue,
    fill_lock,
    live_sources,
    read_backlog,
    write_sent,
)

MANAGED_KINDS = ("process-fix", "defect", "readback")


def load_policy() -> ModelPillarBacklogPolicy:
    """An explicit deployment policy wins over the portable node declaration."""
    from omnimarket.handlers.node_overlay_reader import find_node_overlay

    if find_node_overlay("idle_slot_fill") is not None:
        document = load_structured_overlay("idle_slot_fill", ModelIdleSlotFillOverlay)
        if document.backlog_refresh is not None:
            return document.backlog_refresh
    declaration = Path(__file__).parents[1] / "backlog_refresh.yaml"
    return ModelPillarBacklogPolicy.model_validate(
        yaml.safe_load(declaration.read_text(encoding="utf-8"))
    )


def stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("source timestamp must carry a timezone")
    return parsed.astimezone(UTC)


def _refs(item: dict[str, Any]) -> set[str]:
    text = " ".join(str(item.get(k) or "") for k in ("pr", "key", "title", "fact"))
    return {f"{m['repo']}#{m['n']}" for m in _PR_REF.finditer(text)}


def _tickets(text: str) -> set[str]:
    return {
        ticket.upper() for ticket in re.findall(r"\bOMN-\d+\b", text, re.IGNORECASE)
    }


def _pr_key(key: str) -> str:
    return key.split("/", 1)[-1].lower()


def _pr_retirement(
    item: dict[str, Any], inventory: dict[str, Any], coverage: dict[str, Any]
) -> str:
    for ref in sorted(_refs(item)):
        facts = (inventory.get(ref) or {}).get("facts") or {}
        if facts.get("state") in ("MERGED", "CLOSED"):
            return f"pr-{facts['state'].lower()} {ref}"
        if ref not in inventory:
            if ref.split("#", 1)[0] not in coverage:
                raise ValueError(f"PR watcher coverage unread: {ref}")
            return f"pr-not-open {ref}"
    return ""


def _live_pr_claims(now: datetime, policy: ModelPillarBacklogPolicy) -> set[str]:
    directory = default_claims_dir()
    if directory is None:
        raise ValueError("PR claim registry directory is not configured")
    # A configured, absent directory is the registry's empty initial state.
    try:
        directory.stat()
    except FileNotFoundError:
        return set()
    owned: set[str] = set()
    for path in sorted(p for p in directory.iterdir() if p.suffix == ".json"):
        claim = json.loads(path.read_text(encoding="utf-8"))
        claimed = stamp(claim["claimed_at"])
        heartbeat = stamp(claim.get("last_heartbeat_at") or claim["claimed_at"])
        if (now - heartbeat).total_seconds() <= policy.claim_heartbeat_seconds or (
            now - claimed
        ).total_seconds() <= policy.claim_max_age_seconds:
            owned.add(_pr_key(claim["pr_key"]))
    return owned


_TICKET_QUERY = """
query PillarBacklogLive($first: Int!, $after: String, $filter: IssueFilter!) {
  viewer { id }
  issues(first: $first, after: $after, filter: $filter) {
    nodes {
      identifier title description
      state { type name }
      assignee { id }
      labels { nodes { name } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def _live_ticket_snapshot(
    now: str, readbacks: dict[str, str], policy: ModelPillarBacklogPolicy, post: Post
) -> dict[str, Any]:
    alternatives: list[dict[str, Any]] = []
    if "defect" in policy.sources and policy.defect_labels:
        alternatives.append(
            {"labels": {"some": {"name": {"in": list(policy.defect_labels)}}}}
        )
    if "readback" in policy.sources and readbacks:
        alternatives.append(
            {
                "number": {
                    "in": [
                        int(ticket.split("-", 1)[1])
                        for ticket in sorted(readbacks)
                        if re.fullmatch(r"OMN-\d+", ticket)
                    ]
                }
            }
        )
    snapshot: dict[str, Any] = {"captured_at": now, "issues": [], "operator": None}
    if not alternatives:
        return snapshot
    variables: dict[str, object] = {
        "first": policy.ticket_page_size,
        "after": None,
        "filter": {
            "team": {"key": {"eq": policy.ticket_team_key}},
            "state": {"type": {"nin": ["completed", "canceled"]}},
            "or": alternatives,
        },
    }
    for _ in range(policy.ticket_max_pages):
        data = post(_TICKET_QUERY, variables)
        issues = data.get("issues")
        viewer = data.get("viewer")
        if (
            not isinstance(issues, dict)
            or not isinstance(issues.get("nodes"), list)
            or not isinstance(viewer, dict)
            or not isinstance(viewer.get("id"), str)
        ):
            raise ValueError("live ticket source returned malformed inventory")
        snapshot["operator"] = viewer["id"]
        for issue in issues["nodes"]:
            if (
                not isinstance(issue, dict)
                or not isinstance(issue.get("identifier"), str)
                or not isinstance(issue.get("state"), dict)
            ):
                raise ValueError("live ticket source returned malformed ticket")
            snapshot["issues"].append(issue)
        page = issues.get("pageInfo")
        if not isinstance(page, dict) or not isinstance(page.get("hasNextPage"), bool):
            raise ValueError("live ticket source pagination unread")
        if not page["hasNextPage"]:
            return snapshot
        cursor = page.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor == variables["after"]:
            raise ValueError("live ticket source cursor unread")
        variables["after"] = cursor
    raise ValueError("live ticket source incomplete: page limit reached")


class HandlerPillarBacklogRefresh:
    """The refresh operation of node_idle_slot_fill_effect."""

    def __init__(self, policy: ModelPillarBacklogPolicy | None = None) -> None:
        self._policy = policy

    def handle(
        self, request: ModelPillarBacklogRefreshRequest
    ) -> ModelPillarBacklogRefreshResult:
        if request.read_only:
            return self.refresh_locked(request)
        with fill_lock(request.state_dir) as held:
            if held:
                return self.refresh_locked(request)
        return ModelPillarBacklogRefreshResult(error="lab-fill.lock unavailable")

    def refresh_locked(
        self, request: ModelPillarBacklogRefreshRequest
    ) -> ModelPillarBacklogRefreshResult:
        """Called by the tick while it already owns lab-fill.lock through dispatch."""
        dropped_path: Path | None = None
        standing_known: dict[str, str] = {}
        unmanaged_known: list[dict[str, Any]] = []
        try:
            policy = self._policy or load_policy()
            dropped_path = request.state_dir / policy.dropped_file
            # Standing tickets are policy, not stored work: an unread source
            # withholds the items but keeps the child tickets the other
            # dispatch classes need.
            known_items, standing_known, _ = read_backlog(
                request.backlog_path or request.state_dir / policy.backlog_file
            )
            # Kinds this refresh does not derive from live state keep their
            # existing path to the dispatcher when a source is unread.
            unmanaged_known = [
                item for item in known_items if item.get("kind") not in MANAGED_KINDS
            ]
            now = stamp(request.now)
            watcher = request.watcher_state
            if not isinstance(watcher, dict) or not isinstance(
                watcher.get("prs"), dict
            ):
                raise ValueError("PR watcher inventory unread")
            if not isinstance(watcher.get("repos"), dict):
                raise ValueError("PR watcher repository coverage unread")
            if watcher.get("schema") != 1:
                raise ValueError("PR watcher schema unread")
            age = (now - stamp(str(watcher.get("last_tick") or ""))).total_seconds()
            if not 0 <= age <= policy.watcher_max_age_seconds:
                raise ValueError("PR watcher inventory stale or future-dated")
            resync_age = (
                now - stamp(str(watcher.get("last_full_resync") or ""))
            ).total_seconds()
            if not 0 <= resync_age <= policy.watcher_resync_max_age_seconds:
                raise ValueError("PR watcher full inventory stale or future-dated")
            inventory = watcher["prs"]
            for ref, record in inventory.items():
                if (
                    not isinstance(record, dict)
                    or not isinstance(record.get("facts"), dict)
                    or record["facts"].get("state") not in ("OPEN", "MERGED", "CLOSED")
                    or (
                        record.get("ci") is not None
                        and not isinstance(record["ci"], dict)
                    )
                ):
                    raise ValueError(f"PR watcher record unread: {ref}")
            sources = request.sources or live_sources(request.now, os.environ)
            if sources.ledger_tickets is None:
                raise ValueError("live CLAIM and readback source unavailable")
            claimed, readbacks = sources.ledger_tickets()
            owned = (
                sources.pr_claims()
                if sources.pr_claims is not None
                else _live_pr_claims(now, policy)
            )
            owned = {_pr_key(key) for key in owned}
            issues: list[dict[str, Any]] = []
            operator: str | None = None
            if {"defect", "readback"}.intersection(policy.sources):
                if sources.ticket_post is not None:
                    snapshot = _live_ticket_snapshot(
                        request.now, readbacks, policy, sources.ticket_post
                    )
                elif sources.linear_snapshot is not None:
                    snapshot = json.loads(
                        sources.linear_snapshot.read_text(encoding="utf-8")
                    )
                else:
                    raise ValueError("ticket source unavailable")
                raw_issues = (
                    snapshot.get("issues") if isinstance(snapshot, dict) else snapshot
                )
                if not isinstance(raw_issues, list):
                    raise ValueError("ticket snapshot must contain an issues list")
                captured = (
                    (snapshot.get("captured_at") or snapshot.get("generated_at"))
                    if isinstance(snapshot, dict)
                    else None
                )
                if captured:
                    captured_at = stamp(captured)
                elif sources.linear_snapshot is not None:
                    captured_at = datetime.fromtimestamp(
                        sources.linear_snapshot.stat().st_mtime, UTC
                    )
                else:
                    raise ValueError("ticket source timestamp unread")
                if (
                    not 0
                    <= (now - captured_at).total_seconds()
                    <= policy.ticket_max_age_seconds
                ):
                    raise ValueError("ticket snapshot stale or future-dated")
                issues = [
                    _issue({**issue, "id": issue.get("identifier") or issue.get("id")})
                    for issue in raw_issues
                    if isinstance(issue, dict)
                ]
                operator = (
                    snapshot.get("operator") if isinstance(snapshot, dict) else None
                )
            path = request.backlog_path or request.state_dir / policy.backlog_file
            reserved = {
                (request.state_dir / name).resolve() for name in (LOCK_FILE, SENT_FILE)
            }
            if (
                path.resolve() in reserved
                or dropped_path.resolve() in reserved
                or path.resolve() == dropped_path.resolve()
            ):
                raise ValueError(
                    "refresh paths collide with lock, dispatch history or each other"
                )
            if path.is_file():
                document = json.loads(path.read_text(encoding="utf-8"))
                rows = (
                    document.get("items", [])
                    if isinstance(document, dict)
                    else document
                )
                if not isinstance(rows, list) or any(
                    not isinstance(row, dict) for row in rows
                ):
                    raise ValueError("stored backlog must contain an items list")
            previous, standing, error = read_backlog(path)
            if error:
                raise ValueError(error)
            result = self._rebuild(
                request,
                policy,
                now,
                inventory,
                previous,
                issues,
                claimed,
                owned,
                readbacks,
                operator,
                standing,
            )
            if request.read_only:
                return replace(result, dropped_path=dropped_path)
            drops_path = dropped_path
            recorded = (
                json.loads(drops_path.read_text(encoding="utf-8"))
                if drops_path.is_file()
                else {}
            )
            if not isinstance(recorded, dict):
                raise ValueError("drop record must be a mapping")
            recorded.update(
                {
                    drop["key"]: {"why": drop["why"], "at": request.now}
                    for drop in result.dropped
                }
            )
            write_sent(drops_path, recorded)
            write_sent(
                path,
                {
                    "items": result.items,
                    "standing_tickets": standing,
                    "refreshed_at": request.now,
                },
            )
            return ModelPillarBacklogRefreshResult(
                result.items,
                standing,
                result.dropped,
                written=True,
                dropped_path=dropped_path,
            )
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            ImportError,
            yaml.YAMLError,
            LabFillLiveReadError,
        ) as exc:
            return ModelPillarBacklogRefreshResult(
                items=unmanaged_known,
                standing_tickets=standing_known,
                error=f"{type(exc).__name__}: {exc}",
                dropped_path=dropped_path,
            )

    def _rebuild(
        self,
        request: ModelPillarBacklogRefreshRequest,
        policy: ModelPillarBacklogPolicy,
        now: datetime,
        inventory: dict[str, Any],
        previous: list[dict[str, Any]],
        issues: list[dict[str, Any]],
        claimed: set[str],
        owned: set[str],
        readbacks: dict[str, str],
        operator: str | None,
        standing: dict[str, str],
    ) -> ModelPillarBacklogRefreshResult:
        items: dict[str, dict[str, Any]] = {}
        dropped: list[dict[str, str]] = []
        open_tickets: set[str] = set()
        reasons: dict[str, str] = {}
        previous_keys: dict[tuple[str, str], str] = {}
        for old in previous:
            kind, key = str(old.get("kind") or ""), str(old.get("key") or "")
            refs = _refs(old)
            identity = (
                next(iter(refs))
                if kind == "process-fix" and len(refs) == 1
                else str(old.get("ticket") or "")
            )
            if identity and key:
                previous_keys.setdefault((kind, identity), key)
        for ref, record in sorted(inventory.items()):
            facts = record.get("facts") or {}
            if facts.get("state") != "OPEN":
                continue
            title = str(facts.get("title") or "")
            named = _tickets(title + " " + str(facts.get("head_ref") or ""))
            named.update(
                ticket.upper()
                for ticket in re.findall(
                    r"\bCloses\s+(OMN-\d+)\b",
                    str(facts.get("body") or ""),
                    re.IGNORECASE,
                )
            )
            open_tickets.update(named)
            if "process-fix" not in policy.sources:
                continue
            why = ""
            ci = record.get("ci") or {}
            red = (
                ci.get("verdict") == "RED"
                and ci.get("sha") == facts.get("head_sha")
                and bool(facts.get("head_sha"))
            )
            updated = facts.get("updated_at") or facts.get("created_at")
            stalled = (
                record.get("cls") in policy.stalled_classes
                and bool(updated)
                and (now - stamp(str(updated))).total_seconds()
                >= policy.stalled_seconds
            )
            if _pr_key(ref) in owned or named.intersection(claimed):
                why = "owned"
            elif not red and not stalled:
                why = "checks-not-red-or-stalled"
            if why:
                reasons[ref] = why
                continue
            item = {
                "kind": "process-fix",
                "key": previous_keys.get(("process-fix", ref), ref),
                "repo": ref.split("#", 1)[0],
                "pr": ref,
                "title": title,
                "fact_at": request.now,
                "fact": f"{ref}: {record.get('reason') or ('checks red' if red else 'stalled')}; no live owner",
            }
            if named:
                item["ticket"] = sorted(named)[0]
            items[ref] = item
        for issue in sorted(issues, key=lambda i: i["id"]):
            ticket = issue["id"]
            if not isinstance(ticket, str) or not ticket:
                continue
            if _closed(issue):
                reasons[ticket] = "ticket-closed"
                continue
            if not issue["status"] and not issue["statusType"]:
                reasons[ticket] = "ticket-state-unread"
                continue
            if not re.fullmatch(r"OMN-\d+", ticket):
                continue
            kind = "readback" if ticket in readbacks else "defect"
            if kind not in policy.sources:
                continue
            if kind == "defect" and not set(
                map(str.lower, issue["labels"])
            ).intersection(policy.defect_labels):
                continue
            why = (
                "owned"
                if ticket in claimed
                else "open-pr"
                if ticket in open_tickets
                else "assigned-other"
                if issue["assignee"] and issue["assignee"] not in (operator, "me")
                else ""
            )
            if why:
                reasons[ticket] = why
                continue
            key = f"{kind}:{ticket}"
            items[key] = {
                "kind": kind,
                "key": previous_keys.get((kind, ticket), key),
                "ticket": ticket,
                "repo": issue["repo"],
                "title": issue["title"],
                "fact_at": request.now,
                "fact": f"{ticket}: {readbacks[ticket] if kind == 'readback' else issue['title']}",
            }
        coverage = (request.watcher_state or {}).get("repos", {})
        for identity, item in list(items.items()):
            if why := _pr_retirement(item, inventory, coverage):
                dropped.append({"key": item["key"], "why": why})
                reasons[identity] = why
                del items[identity]
        for old in previous:
            why = _pr_retirement(old, inventory, coverage)
            refs = _refs(old)
            if not why and old.get("kind") in MANAGED_KINDS:
                if refs and any(ref in items for ref in refs):
                    continue
                ticket = str(old.get("ticket") or "")
                if (
                    any(item.get("ticket") == ticket for item in items.values())
                    and ticket
                ):
                    continue
                why = next(
                    (reasons[ref] for ref in sorted(refs) if ref in reasons),
                    reasons.get(ticket, "source-no-longer-offers-item"),
                )
            if not why:
                fact = _fact_newest(old)
                if (
                    fact is not None
                    and (now.replace(tzinfo=None) - fact[0]).total_seconds()
                    > policy.fact_max_age_seconds
                ):
                    why = f"fact-stale {fact[1]} (older than 48h)"
            if why:
                dropped.append({"key": str(old.get("key") or ""), "why": why})
            else:
                items[str(old["key"])] = old
        order = {str(item.get("key")): index for index, item in enumerate(previous)}
        projected = sorted(
            items.values(), key=lambda item: order.get(item["key"], len(order))
        )
        unique_drops = {row["key"]: row for row in dropped}
        return ModelPillarBacklogRefreshResult(
            projected, standing, list(unique_drops.values())
        )
