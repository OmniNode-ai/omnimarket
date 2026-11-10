# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closeout sweep's scope ranking and bundle (OMN-20655).

Ported from the sweep's own closeout scope handler, which ported the dod-closeout-sweep
script's selection, so each function keeps the script's truthiness and String() coercions and
selects what the script would. What it adds is overlay data: the ordered rank rules (a ticket
with a contract on a default branch ranks before one without), the reserve of slots kept for
tickets with no contract, the caps and the eligible states. Performs no read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_closeout_scope_overlay import (
    EnumRankRule,
    ModelCloseoutScopeOverlay,
)

_TEXT_SHA = re.compile(r"^[0-9a-f]{16}$")


class ScopeInputUnreadableError(ValueError):
    """An input that is not what the bundle reads; never an empty bundle."""


def _js_truthy(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, int | float):
        return value == value and value != 0
    if isinstance(value, str):
        return value != ""
    return True


def _js_string(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, list):
        return ",".join("" if x is None else _js_string(x) for x in value)
    return "[object Object]"


def _js_or(*values: Any) -> Any:
    """``a || b || ...``: the first truthy value, else the last."""
    for value in values[:-1]:
        if _js_truthy(value):
            return value
    return values[-1]


def _has_id(row: Any) -> bool:
    return isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"] != ""


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def parse_pr_state(rows: Sequence[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not _has_id(row):
            continue
        valid = _strings(row.get("merged")) and _strings(row.get("open"))
        out.append(
            {
                **row,
                "merged": row["merged"] if _strings(row.get("merged")) else [],
                "open": row["open"] if _strings(row.get("open")) else [],
                "error": _js_or(
                    row.get("error"), "" if valid else "malformed PR state row"
                ),
            }
        )
    return out


def parse_rows(rows: Sequence[Any]) -> list[dict[str, Any]]:
    return [row for row in rows if _has_id(row)]


def with_text_state(
    candidates: Sequence[Any], rows: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {row["id"]: row for row in rows}
    out: list[dict[str, Any]] = []
    for c in candidates:
        cand = c if isinstance(c, dict) else {}
        r = by_id.get(cand.get("id"))
        own = r.get("last_closeout_comment_at") if r else None
        at = _js_or(
            own if isinstance(own, str) else None,
            cand.get("last_closeout_comment_at"),
            "",
        )
        waiting_on = r.get("waiting_on") if r else None
        out.append(
            {
                **cand,
                "last_closeout_comment_at": at,
                "text_sha": _js_string(_js_or(r.get("text_sha") if r else None, "")),
                "held_text_sha": _js_string(
                    _js_or(r.get("held_text_sha") if r else None, "")
                ),
                "waiting_on": waiting_on if _strings(waiting_on) else [],
            }
        )
    return out


def with_pr_state(
    candidates: Sequence[dict[str, Any]], rows: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {row["id"]: row for row in rows}
    out: list[dict[str, Any]] = []
    for c in candidates:
        r = by_id.get(c.get("id"))
        merged = _js_or(r.get("merged") if r else None, [])
        sources: list[Any] = []
        own = c.get("sources")
        for source in [
            *(own if isinstance(own, list) else []),
            *(["merged"] if merged else []),
        ]:
            if source not in sources:
                sources.append(source)
        out.append(
            {
                **c,
                "merged": merged,
                "open": _js_or(r.get("open") if r else None, []),
                "sources": sources,
                "pr_error": _js_string(_js_or(r.get("error"), ""))
                if r
                else "no PR state row",
            }
        )
    return out


def with_contract_state(
    candidates: Sequence[dict[str, Any]], rows: Sequence[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Mark each candidate with whether a contract is on a default branch. A scope with no
    contract reading at all (``None``) marks every ticket as having one, so the ranking is
    the script's own; a ticket the reading does not name reads as no contract."""
    if rows is None:
        return [{**c, "has_contract": True} for c in candidates]
    by_id = {row["id"]: row for row in rows}
    return [
        {**c, "has_contract": by_id.get(c.get("id"), {}).get("has_contract") is True}
        for c in candidates
    ]


def pr_gate(row: Mapping[str, Any], overlay: ModelCloseoutScopeOverlay) -> str:
    unread = _js_or(row.get("pr_error"), row.get("error"))
    if _js_truthy(unread):
        return f"PR state unread: {_js_string(unread)}"
    state = _js_string(_js_or(row.get("state"), "")).strip().lower()
    if state not in overlay.eligible_states:
        return "not started"
    merged = row.get("merged")
    if not isinstance(merged, list) or not merged:
        return "no merged PR references it"
    opened = row.get("open")
    if isinstance(opened, list) and opened:
        return (
            f"open PR(s) still reference it: {', '.join(_js_string(x) for x in opened)}"
        )
    return ""


def held_for_amendment(row: Mapping[str, Any]) -> bool:
    now = _js_string(_js_or(row.get("text_sha"), ""))
    held = _js_string(_js_or(row.get("held_text_sha"), ""))
    return bool(_TEXT_SHA.match(now)) and now == held


def is_merged_in_review(row: Mapping[str, Any]) -> bool:
    sources = row.get("sources")
    state = _js_string(_js_or(row.get("state"), "")).strip().lower()
    return state == "in review" and isinstance(sources, list) and "merged" in sources


def _rule_applies(rule: EnumRankRule, row: Mapping[str, Any]) -> bool:
    if rule is EnumRankRule.WAITING_ON:
        waiting_on = row.get("waiting_on")
        return isinstance(waiting_on, list) and bool(waiting_on)
    if rule is EnumRankRule.HAS_CONTRACT:
        return row.get("has_contract") is True
    return is_merged_in_review(row)


def rank_key(
    row: Mapping[str, Any], overlay: ModelCloseoutScopeOverlay
) -> tuple[Any, ...]:
    """One 0-or-1 entry per overlay rule, in the overlay's order, then the oldest closeout
    comment, then the id. A ticket waiting on a post-merge condition is one group: the rules
    after waiting_on do not reorder it, so rules [waiting_on, merged_in_review] rank
    exactly as the script's three tiers."""
    parts: list[int] = []
    decided = False
    for rule in overlay.rank_rules:
        if decided:
            parts.append(0)
            continue
        hit = _rule_applies(rule, row)
        parts.append(0 if hit else 1)
        decided = hit and rule is EnumRankRule.WAITING_ON
    at = row.get("last_closeout_comment_at")
    return (*parts, at if isinstance(at, str) else "", row["id"])


def select_candidates(
    records: Sequence[dict[str, Any]], limit: int, overlay: ModelCloseoutScopeOverlay
) -> dict[str, Any]:
    """selected, deferred, held and waiting over the merged rows."""
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for row in records:
        if _has_id(row) and row["id"] not in seen:
            seen.add(row["id"])
            rows.append(row)
    waiting = sorted(
        (
            {"id": r["id"], "reason": reason}
            for r in rows
            if (reason := pr_gate(r, overlay))
        ),
        key=lambda w: w["id"],
    )
    eligible = [r for r in rows if not pr_gate(r, overlay)]
    held = sorted(r["id"] for r in eligible if held_for_amendment(r))
    ranked = sorted(
        (r for r in eligible if not held_for_amendment(r)),
        key=lambda r: rank_key(r, overlay),
    )
    ids = _apply_reserve(
        ranked, limit, overlay.no_contract_reserve, overlay.no_contract_only_by_reserve
    )
    chosen = set(ids)
    return {
        "selected": ids,
        "deferred": [r["id"] for r in ranked if r["id"] not in chosen],
        "held": held,
        "waiting": waiting,
        "candidates": len(rows),
        "eligible": len(eligible),
    }


def _is_contractless(row: Mapping[str, Any]) -> bool:
    """No contract on a default branch and no recorded post-merge condition: a ticket
    dod_verify cannot run for and no earlier adjudication is waiting to re-check."""
    return row.get("has_contract") is False and not _rule_applies(
        EnumRankRule.WAITING_ON, row
    )


def _apply_reserve(
    ranked: Sequence[dict[str, Any]],
    limit: int,
    reserve: int,
    only_by_reserve: bool = False,
) -> list[str]:
    """The top ``limit`` of the ranking, except that up to ``reserve`` of those slots go to
    the best-ranked tickets with no contract, so the binder can still author a contract
    for them and the has_contract rule cannot starve them. Order stays the ranking's.
    With ``only_by_reserve`` a ticket with no contract takes no other slot: the run holds the
    verifiable tickets and the reserve, and is smaller when fewer than ``limit`` can verify."""
    position = {r["id"]: n for n, r in enumerate(ranked)}
    if only_by_reserve:
        kept = [r for r in ranked if not _is_contractless(r)]
        reserved = [r for r in ranked if _is_contractless(r)][:reserve]
        base = kept[: max(limit - len(reserved), 0)]
        picked = [*base, *reserved][:limit]
        return sorted((r["id"] for r in picked), key=lambda i: position[i])
    head = [r["id"] for r in ranked[:limit]]
    if not reserve or limit <= reserve or not ranked:
        return head
    without = [r["id"] for r in ranked if r.get("has_contract") is False]
    if not without:
        return head
    keep = limit - min(reserve, len(without))
    base_ids = [r["id"] for r in ranked[:keep]]
    taken = set(base_ids)
    slots = limit - len(base_ids)
    extra = [i for i in without if i not in taken][:slots]
    if len(extra) < slots:
        extra += [r["id"] for r in ranked[keep:] if r["id"] not in extra][
            : slots - len(extra)
        ]
    return sorted([*base_ids, *extra], key=lambda i: position[i])


def _encoded(obj: Mapping[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def scope_bundle(
    candidates: Sequence[Any],
    pr_state: Sequence[Any],
    text_state: Sequence[Any],
    contract_state: Sequence[Any] | None,
    limit: int,
    overlay: ModelCloseoutScopeOverlay,
    *,
    held_cap: int | None = None,
    waiting_cap: int | None = None,
) -> dict[str, Any]:
    """The bounded scope: the top ``limit`` tickets' rows, held, waiting and totals."""
    held_cap = overlay.held_cap if held_cap is None else held_cap
    waiting_cap = overlay.waiting_cap if waiting_cap is None else waiting_cap
    pr_rows = parse_pr_state(pr_state)
    text_rows = parse_rows(text_state)
    contract_rows = None if contract_state is None else parse_rows(contract_state)
    merged = with_contract_state(
        with_pr_state(with_text_state(candidates, text_rows), pr_rows), contract_rows
    )
    picked = select_candidates(merged, limit, overlay)
    first: dict[str, Any] = {}
    for c in candidates:
        if _has_id(c):
            first.setdefault(c["id"], c)
    # The script indexes rows by id and the last row wins, so the bundle carries that one.
    last_pr = {r["id"]: r for r in pr_state if _has_id(r)}
    last_text = {r["id"]: r for r in text_state if _has_id(r)}
    by_id = {r["id"]: r for r in merged}
    selected = picked["selected"]
    by_reason: dict[str, int] = {}
    for row in picked["waiting"]:
        reason = row["reason"].split(": ", 1)[0]
        by_reason[reason] = by_reason.get(reason, 0) + 1
    no_contract = [i for i in selected if by_id[i].get("has_contract") is False]
    return {
        "candidates": [first[i] for i in selected],
        "pr_state": [last_pr[i] for i in selected if i in last_pr],
        "text_state": [last_text[i] for i in selected if i in last_text],
        "held": picked["held"][:held_cap],
        "held_count": len(picked["held"]),
        "waiting": picked["waiting"][:waiting_cap],
        "waiting_count": len(picked["waiting"]),
        "waiting_by_reason": dict(sorted(by_reason.items())),
        "no_contract": no_contract,
        "totals": {
            "candidates": picked["candidates"],
            "eligible": picked["eligible"],
            "selected": len(selected),
            "deferred": len(picked["deferred"]),
            "held": len(picked["held"]),
            "waiting": len(picked["waiting"]),
        },
    }


def bundle_text(
    candidates: Sequence[Any],
    pr_state: Sequence[Any],
    text_state: Sequence[Any],
    contract_state: Sequence[Any] | None,
    limit: int,
    overlay: ModelCloseoutScopeOverlay,
) -> str:
    """The bundle as printed: the listed held and waiting ids shrink before it may pass the
    overlay's byte cap (their counts stay exact); the selected rows never do."""
    held_cap, waiting_cap = overlay.held_cap, overlay.waiting_cap
    while True:
        text = _encoded(
            scope_bundle(
                candidates,
                pr_state,
                text_state,
                contract_state,
                limit,
                overlay,
                held_cap=held_cap,
                waiting_cap=waiting_cap,
            )
        )
        if len(text.encode("utf-8")) <= overlay.bundle_max_bytes:
            return text
        if not held_cap and not waiting_cap:
            raise ScopeInputUnreadableError(
                f"the rows of the top {limit} tickets alone are {len(text.encode('utf-8'))} "
                f"bytes, over {overlay.bundle_max_bytes}; lower --max"
            )
        held_cap, waiting_cap = held_cap // 2, waiting_cap // 2
