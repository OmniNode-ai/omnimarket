# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The idle-turn guard's decision (OMN-19998): pure, over the turn's finding lines and the adapter's facts.

On 2026-11-04 the guard blocked 33 Stop turns. Five came after the operator wrote stop. Each one listed hosts that
refuse lanes as free headroom. It also blocked a turn whose send had started in the background, which no tool call
in the turn showed. The rule, in order:

1. Nothing to answer (no headroom, red or broken line left) passes.
2. A hold or stop row in force holds the guard (verdict ``held``). The turn is not refused.
3. A headroom line for a host that refuses lanes is dropped. That host is not headroom.
4. A tool call that dispatched, or a runner receipt in the window whose state the contract counts as a dispatch
   (``config.idle_turn_guard.dispatch_receipt_states``: placed, placing), passes.
5. Anything left refuses.

The policy is read from ``node_idle_turn_guard_compute``'s contract once and passed in. This module reads no file,
environment or clock while it decides.
"""

from __future__ import annotations

import re
from importlib.resources import files

import yaml

from omnimarket.models.idle_turn_guard import (
    EnumIdleTurnGuardVerdict,
    ModelIdleTurnGuardDecision,
    ModelIdleTurnGuardDecisionRequest,
    ModelIdleTurnGuardPolicy,
)

_NODE_PACKAGE = "omnimarket.nodes.node_idle_turn_guard_compute"
HEADROOM_LINE = re.compile(r"^MISSING lab-headroom:(?P<host>\S+) ")


def load_idle_turn_guard_policy() -> ModelIdleTurnGuardPolicy:
    """The packaged node contract's ``config.idle_turn_guard`` block."""
    raw = yaml.safe_load(
        files(_NODE_PACKAGE).joinpath("contract.yaml").read_text("utf-8")
    )
    return ModelIdleTurnGuardPolicy.model_validate(raw["config"]["idle_turn_guard"])


def decide_idle_turn(
    request: ModelIdleTurnGuardDecisionRequest, policy: ModelIdleTurnGuardPolicy
) -> ModelIdleTurnGuardDecision:
    facts = request.facts
    refusing = frozenset(facts.lane_refusing_hosts)
    headroom: list[str] = []
    discounted: list[str] = []
    for line in request.headroom_lines:
        m = HEADROOM_LINE.match(line)
        host = m["host"] if m else ""
        if host and host in refusing:
            if host not in discounted:
                discounted.append(host)
            continue
        headroom.append(line)
    shown = (*headroom, *request.unowned_lines, *request.broken_lines)
    counts = {
        "headroom_count": len(headroom),
        "unowned_count": len(request.unowned_lines),
        "broken_count": len(request.broken_lines),
        "discounted_hosts": tuple(discounted),
    }
    if not shown:
        return ModelIdleTurnGuardDecision(
            verdict=EnumIdleTurnGuardVerdict.PASS, shown=shown, **counts
        )
    holds = tuple(h.id for h in facts.holds_in_force)
    if holds:
        return ModelIdleTurnGuardDecision(
            verdict=EnumIdleTurnGuardVerdict.HELD,
            reason=(
                f"held: {len(holds)} hold or stop row(s) in force ({', '.join(holds)}); "
                "an idle turn is not refused while dispatch is held"
            ),
            shown=shown,
            hold_ids=holds,
            **counts,
        )
    counted = frozenset(policy.dispatch_receipt_states)
    lanes = (
        tuple(
            r.lane
            for r in facts.runner_receipts
            if r.state in counted and facts.window_start <= r.at <= request.now
        )
        if facts.window_start is not None
        else ()
    )
    if request.tool_dispatched or lanes:
        return ModelIdleTurnGuardDecision(
            verdict=EnumIdleTurnGuardVerdict.PASS,
            reason=(
                f"dispatched: runner receipt(s) in the window for {', '.join(lanes)}"
                if lanes and not request.tool_dispatched
                else ""
            ),
            shown=shown,
            receipt_lanes=lanes,
            **counts,
        )
    return ModelIdleTurnGuardDecision(
        verdict=EnumIdleTurnGuardVerdict.REFUSE,
        reason=f"{len(shown)} finding line(s) read and nothing dispatched",
        shown=shown,
        **counts,
    )


__all__ = ["HEADROOM_LINE", "decide_idle_turn", "load_idle_turn_guard_policy"]
