# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A named host is placement only after runner setup passed, so retries do not double count."""

import re
from collections.abc import Mapping

from ..models.model_lab_fill_placement import (
    ModelLabFillPlacementRequest,
    ModelLabFillPlacementResult,
)
from .helpers_js_value import truthy
from .helpers_outcomes import by_lane, number, string, text

CODEX_ENGINE_FAILURES = (
    "codex-sandbox-unavailable",
    "codex-missing",
    "codex-not-logged-in",
)
WINDOW_OUTCOME_RE = re.compile(r"^skipped:(deadline|lane-timeout)")


def placement(
    planned: Mapping[str, object],
    dispatched: Mapping[str, object] | None,
    receipt: Mapping[str, object] | None,
) -> dict[str, object]:
    """Keep refusals, missing receipts and pin mismatches distinct."""
    at = {k: planned[k] for k in ("lane", "host", "engine") if k in planned}
    d, r = dispatched or {}, receipt or {}

    def result(outcome: str, detail: object, host: object = None) -> dict[str, object]:
        return {
            **at,
            **({"host": host} if host is not None else {}),
            "outcome": outcome,
            "detail": detail,
        }

    if not dispatched or not truthy(d.get("detached")):
        return result(
            "skipped:" + text(d.get("skipped"))
            if truthy(d.get("skipped"))
            else "not-started",
            d.get("detail") if truthy(d.get("detail")) else "",
        )
    if not receipt or not truthy(r.get("found")):
        return result("pending", "no receipt read")
    status = text(r.get("status"))
    if re.search(r"no-host|usage|refused|mismatch|omni-home-unresolved", status):
        if planned.get("pinned") is True and "no-host" in status:
            return result(
                "deferred:pinned-host-refused",
                f"host={text(planned.get('host'))} status={status}",
            )
        return result("refused", f"status={status}")
    if status in CODEX_ENGINE_FAILURES:
        exit_code = r.get("exit_code")
        return result(
            "engine-failed:" + status,
            f"status={status}"
            + (" exit=" + string(exit_code) if exit_code is not None else ""),
        )
    if not truthy(r.get("host")):
        return result("pending", "status=" + (status or "unknown"))
    if planned.get("pinned") is not True:
        return result("placed", "status=" + status, r["host"])
    if r.get("host") != planned.get("host"):
        return result(
            "placed-elsewhere", f"receipt host={string(r.get('host'))} status={status}"
        )
    return result("placed", "status=" + status)


class HandlerLabFillPlacement:
    """Verify receipt facts and name the lane that consumed the dispatch window."""

    def handle(
        self, request: ModelLabFillPlacementRequest
    ) -> ModelLabFillPlacementResult:
        ds, rs = by_lane(request.dispatched), by_lane(request.receipts)
        ps = [
            placement(p, ds.get(text(p.get("lane"))), rs.get(text(p.get("lane"))))
            for p in request.planned
        ]
        items = by_lane(request.planned)
        deferred = []
        for p in ps:
            item = items.get(text(p.get("lane")))
            if p["outcome"] == "deferred:pinned-host-refused" and item is not None:
                deferred.append(
                    {
                        "id": item.get("pr")
                        if truthy(item.get("pr"))
                        else item.get("ticket"),
                        **({"kind": item.get("kind")} if request.project_id else {}),
                        "reason": "pinned-host-refused",
                        "host": item.get("host"),
                        "lane": item.get("lane"),
                    }
                )
        failure = None
        if ps and all(WINDOW_OUTCOME_RE.match(text(p["outcome"])) for p in ps):
            timed = [
                d
                for d in request.dispatched
                if d
                and any(p.get("lane") == d.get("lane") for p in ps)
                and number(d.get("elapsed_s")) > 0
            ]
            longest = (
                max(timed, key=lambda d: number(d.get("elapsed_s"))) if timed else None
            )
            lane = longest.get("lane") if longest else ps[0].get("lane")
            elapsed = number(longest.get("elapsed_s")) if longest else None
            cut = sum(text(p["outcome"]).startswith("skipped:deadline") for p in ps)
            suffix = (
                " (no timings returned: the first lane started)"
                if elapsed is None
                else f" ({string(elapsed)} s)"
            )
            failure = {
                "code": "DISPATCH_WINDOW_CONSUMED",
                "lane": lane,
                "elapsed_s": elapsed,
                "message": f"lab-fill: DISPATCH_WINDOW_CONSUMED — placed 0 of {len(ps)} lane(s): {cut} cut at the deadline, {len(ps) - cut} at their slice. Lane {lane} consumed the window{suffix}.",
            }
        return ModelLabFillPlacementResult(
            placements=tuple(ps), deferred=tuple(deferred), failure=failure
        )
