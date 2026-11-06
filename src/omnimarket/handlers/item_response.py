# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DispatchAnswer and LaneAnswers: the same predicate for S5 and G2."""

from omnimarket.models.item_state_check import (
    ModelItem,
    ModelItemPolicy,
    ModelItemSnapshot,
)


def delivered_response(
    item: ModelItem, snapshot: ModelItemSnapshot, policy: ModelItemPolicy
) -> bool:
    current = tuple(
        d
        for d in item.deliveries
        if d.digest == item.digest and (d.accepted or d.printed)
    )
    if item.owner == policy.operator_owner:
        return any(d.kind == "question" for d in current)
    if any(
        d.kind == "reply"
        or (
            d.kind == "queue"
            and d.tick_id == snapshot.tick_id
            and d.position is not None
            and d.reason is not None
        )
        for d in current
    ):
        return True
    park = item.park
    return bool(
        item.state == policy.parked_state
        and park
        and snapshot.now < park.review_by
        and any(
            a.kind == park.authority_kind and a.stamp == park.authority_stamp
            for a in snapshot.authorities
        )
    )


def item_answered(
    item: ModelItem, snapshot: ModelItemSnapshot, policy: ModelItemPolicy
) -> bool:
    if item.state in policy.closed_states:
        return True
    return delivered_response(item, snapshot, policy) or bool(
        item.owner != policy.operator_owner and item.running_lane
    )


def g2_unanswered(
    snapshot: ModelItemSnapshot, policy: ModelItemPolicy
) -> tuple[str, ...]:
    return tuple(
        item.item_id
        for item in snapshot.items
        if not item_answered(item, snapshot, policy)
    )
