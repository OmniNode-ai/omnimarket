# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One eligibility predicate for R-D fill priority and item-bound Stop accounting."""

from omnimarket.models.standing_rules.model_rule_cause_idle import (
    ModelProcessFixInput,
    ModelProcessFixItem,
    ModelProcessFixStopRequest,
    ModelProcessFixStopResult,
)


def eligible_process_fixes(
    source: ModelProcessFixInput,
) -> tuple[ModelProcessFixItem, ...]:
    rule = source.rule
    seen: set[str] = set()
    out = []
    for item in source.items:
        if (
            item.item_id not in seen
            and item.kind in rule.item_kinds
            and item.ruling_kind == rule.ruling_kind
            and item.state not in rule.closed_states
            and not item.running_lane
        ):
            seen.add(item.item_id)
            out.append(item)
    return tuple(out)


class HandlerProcessFixStop:
    def handle(self, request: ModelProcessFixStopRequest) -> ModelProcessFixStopResult:
        eligible = {
            item.item_id for item in eligible_process_fixes(request.process_fixes)
        }
        # Count both lanes and items once. A replayed CLAIM or one lane naming two
        # items does not consume two free slots.
        lanes: set[str] = set()
        items: set[str] = set()
        for start in request.accepted_starts:
            bound = next(
                (
                    item
                    for item in (start.for_item, start.ask)
                    if item in eligible and item not in items
                ),
                None,
            )
            if bound is not None and start.lane not in lanes:
                items.add(bound)
                lanes.add(start.lane)
        required = min(request.free_slots, len(eligible))
        refused = len(lanes) < required
        return ModelProcessFixStopResult(
            refuse=refused,
            required=required,
            bound_starts=len(lanes),
            reason=(
                f"{request.process_fixes.rule.id}: {required} process-fix item-bound starts required; {len(lanes)} accepted; {request.process_fixes.rule.fix}"
                if refused
                else ""
            ),
        )
