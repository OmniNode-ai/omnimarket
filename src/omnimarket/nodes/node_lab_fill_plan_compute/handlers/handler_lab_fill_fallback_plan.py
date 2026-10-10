# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reserve one Claude retry only when Codex never ran; keep ranked live choices separate."""

from ..models.model_lab_fill_fallback_plan import (
    ModelLabFillFallbackPlanRequest,
    ModelLabFillFallbackPlanResult,
)
from ..models.model_lab_fill_lane_render import ModelLabFillFallbackItem
from .handler_lab_fill_dispatch_plan import APPROVED_KINDS, LANE_ROUTES
from .handler_lab_fill_placement import placement
from .helpers_js_value import truthy
from .helpers_outcomes import by_lane, pick_host, text


class HandlerLabFillFallbackPlan:
    """Account for primary reservations before ranking hosts for a single retry."""

    def handle(
        self, request: ModelLabFillFallbackPlanRequest
    ) -> ModelLabFillFallbackPlanResult:
        items, outcomes = by_lane(request.planned), by_lane(request.placements)
        left = {
            h.name: h.lanes for h in request.capacity.hosts if h.lanes > 0 and h.claude
        }
        loads = {
            h.name: h.load_per_core if h.load_per_core is not None else 9
            for h in request.capacity.hosts
        }

        def failed(lane: str) -> bool:
            return text(outcomes.get(lane, {}).get("outcome")).startswith(
                "engine-failed:"
            )

        for lane, primary in items.items():
            host = text(primary.get("host"))
            if host in left and not failed(lane):
                left[host] -= 1
        fallbacks = []
        skipped: list[dict[str, object]] = []
        for lane, outcome in outcomes.items():
            item = items.get(lane)
            if (
                not failed(lane)
                or item is None
                or item.get("engine") != "codex"
                or truthy(item.get("fallback_of"))
                or lane.endswith("-fb")
            ):
                continue
            ranked = [
                h
                for h, slots in left.items()
                if slots > 0
                and (item.get("pinned") is not True or h == item.get("host"))
            ]
            ranked.sort(key=lambda h: (-left[h], loads[h], h))
            if not ranked:
                skipped.append(
                    {
                        "lane": lane,
                        "reason": "pinned-host-no-claude"
                        if item.get("pinned") is True
                        else "no-claude-host",
                    }
                )
                continue
            left[ranked[0]] -= 1
            fields: dict[str, object] = {
                "lane": lane + "-fb",
                "from_lane": lane,
                "from_engine": "codex",
                "from_host": text(item.get("host")),
                "engine": "sonnet",
                "reason": text(outcome.get("outcome"))[len("engine-failed:") :],
                "route": LANE_ROUTES["sonnet"],
                "hosts": ranked,
                "pinned": item.get("pinned") is True,
                "kind": text(item.get("kind")),
                "ticket": text(item.get("ticket")),
                "fallback_of": lane,
                **{
                    k: text(item.get(k))
                    for k in ("pr", "repo", "ref", "title", "updatedAt")
                },
            }
            if item.get("kind") in APPROVED_KINDS and "id" in item:
                fields["id"] = item["id"]
            fallbacks.append(ModelLabFillFallbackItem.model_validate(fields))
        returned = by_lane(request.returned)
        results: list[dict[str, object]] = []
        for f in fallbacks:
            entry = returned.get(f.lane)
            receipt_host = (
                entry.get("host") if entry and truthy(entry.get("host")) else ""
            )
            verdict = (
                placement(
                    {
                        "lane": f.lane,
                        "host": receipt_host,
                        "pinned": f.pinned,
                        "engine": f.engine,
                    },
                    entry,
                    entry,
                )
                if entry
                else {"outcome": "not-started", "detail": ""}
            )
            results.append(
                {
                    "lane": f.lane,
                    "from_lane": f.from_lane,
                    "from_engine": f.from_engine,
                    "engine": f.engine,
                    "reason": f.reason,
                    "host": receipt_host,
                    "outcome": verdict["outcome"],
                    "detail": verdict["detail"],
                }
            )
        for sk in skipped:
            lane = text(sk["lane"])
            p = next((p for p in request.placements if p.get("lane") == lane), {})
            results.append(
                {
                    "lane": lane + "-fb",
                    "from_lane": lane,
                    "from_engine": "codex",
                    "engine": "sonnet",
                    "reason": text(p.get("outcome")).removeprefix("engine-failed:"),
                    "host": "none",
                    "outcome": "skipped:" + text(sk["reason"]),
                    "detail": "",
                }
            )
        return ModelLabFillFallbackPlanResult(
            fallbacks=tuple(fallbacks),
            skipped=tuple(skipped),
            hosts={f.lane: pick_host(f.hosts, request.limited) for f in fallbacks},
            outcomes=tuple(results),
        )
