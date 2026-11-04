# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick: merge recoveries, claim once, deliver or queue, then check G2."""

from __future__ import annotations

from typing import cast

from omnimarket.handlers.container.container_overlay_dependencies import (
    declared_handler_dependencies,
    require_dependencies,
)
from omnimarket.handlers.item_response import g2_unanswered
from omnimarket.models.item_state_check import (
    ModelItemDelivery,
    ModelItemDispatchReceipt,
    ModelItemPolicy,
    ModelItemRecoveryRequest,
    ModelItemTickRequest,
    ModelItemTickResult,
)
from omnimarket.nodes.node_item_lane_miss_compute import NodeItemLaneMissCompute
from omnimarket.nodes.node_item_recovery_plan_compute import NodeItemRecoveryPlanCompute
from omnimarket.nodes.node_item_unanswered_compute import NodeItemUnansweredCompute
from omnimarket.protocols.protocol_item_recovery import (
    ProtocolItemDispatch,
    ProtocolItemRecoveryStore,
)


class HandlerItemStateCheck:
    def __init__(
        self,
        store: ProtocolItemRecoveryStore | None = None,
        dispatch: ProtocolItemDispatch | None = None,
        policy: ModelItemPolicy | None = None,
    ) -> None:
        self.store = store
        self.dispatch = dispatch
        self.policy = policy or NodeItemUnansweredCompute().policy

    def handle(self, request: ModelItemTickRequest) -> ModelItemTickResult:
        store, io = self.store, self.dispatch
        if store is None or io is None:
            deps = require_dependencies(
                declared_handler_dependencies(
                    "omnimarket.nodes.node_item_state_check_effect"
                )
            )
            store = cast(
                ProtocolItemRecoveryStore,
                deps[
                    "omnimarket.protocols.protocol_item_recovery.ProtocolItemRecoveryStore"
                ],
            )
            self.store = store
            io = cast(
                ProtocolItemDispatch,
                deps[
                    "omnimarket.protocols.protocol_item_recovery.ProtocolItemDispatch"
                ],
            )
            self.dispatch = io
        snapshot = request.snapshot
        items = []
        for item in snapshot.items:
            delivery = store.delivery(item.family)
            if delivery is not None:
                item = item.model_copy(
                    update={"deliveries": (*item.deliveries, delivery)}
                )
            # The durable ACK covers a lagging fold, including a zero-capacity tick.
            claim = store.latest(item.family)
            if (
                claim
                and claim.receipt
                and claim.receipt.accepted
                and claim.receipt.lane
                and item.owner != self.policy.operator_owner
                and (
                    not item.ended_lane or claim.plan.attempt > item.ended_lane.attempt
                )
            ):
                item = item.model_copy(update={"running_lane": claim.receipt.lane})
            items.append(item)
        snapshot = snapshot.model_copy(update={"items": tuple(items)})
        findings = (
            *NodeItemUnansweredCompute(self.policy).handle(snapshot).findings,
            *NodeItemLaneMissCompute(self.policy).handle(snapshot).findings,
        )
        started: list[ModelItemDispatchReceipt] = []
        queued: list[tuple[str, ModelItemDelivery]] = []
        lines: list[str] = []
        free = request.free_slots
        promotions = {p.rule_id: p for p in request.promotions}
        for index, item in enumerate(items):
            own = tuple(
                f
                for f in findings
                if f.item_id == item.item_id and f.digest == item.digest
            )
            if not own:
                line = f"{item.item_id} age={item.age_minutes:g} " + (
                    f"owned by {item.running_lane}" if item.running_lane else "answered"
                )
                io.print_line(line)
                lines.append(line)
                continue
            plan = NodeItemRecoveryPlanCompute(self.policy).handle(
                ModelItemRecoveryRequest(
                    item=item, findings=own, recommendations=request.recommendations
                )
            )
            # Rows stay shadow until Part A emits a measured promotion for them.
            enabled = any(
                f.rule_id in promotions and promotions[f.rule_id].mode == "dispatch"
                for f in own
            )
            if not enabled:
                line = (
                    f"{item.item_id} would-have-sent {plan.action} cause={plan.cause}"
                )
                io.print_line(line)
                lines.append(line)
                continue
            needs_lane = not (
                plan.escalation or item.owner == self.policy.operator_owner
            )
            if needs_lane and free == 0:
                position = len(queued) + 1
                line = f"STATUS item={item.item_id} state=queued seq={position} reason=capacity"
                printed = io.print_line(line)
                delivery = ModelItemDelivery(
                    digest=item.digest,
                    kind="queue",
                    printed=printed,
                    tick_id=snapshot.tick_id,
                    position=position,
                    reason="capacity",
                )
                store.notice(item.family, delivery)
                items[index] = item.model_copy(
                    update={"deliveries": (*item.deliveries, delivery)}
                )
                queued.append((item.item_id, delivery))
                lines.append(line)
                continue
            acquired, claim = store.claim(plan)
            receipt = claim.receipt
            if acquired or receipt is None:
                # A durable pending intent may be replayed after a crash. The
                # canonical dispatcher must be idempotent under this exact cause.
                receipt = io.dispatch(claim.plan)
                if receipt.cause != plan.cause:
                    raise ValueError("dispatch acknowledged a different cause")
                if needs_lane and (not receipt.accepted or not receipt.lane):
                    raise ValueError("dispatch did not acknowledge a running lane")
                if not needs_lane and not (receipt.accepted or receipt.printed):
                    raise ValueError("operator question was not delivered")
                store.settle(plan, receipt)
                if acquired:
                    started.append(receipt)
                if needs_lane:
                    free -= 1
            if receipt is None:
                line = f"{item.item_id} claim pending cause={plan.cause}"
            elif needs_lane:
                items[index] = item.model_copy(update={"running_lane": receipt.lane})
                line = f"{item.item_id} {'started' if acquired else 'owned by'} {receipt.lane}"
            else:
                if plan.escalation:
                    position = len(queued) + 1
                    line = f"STATUS item={item.item_id} state=queued seq={position} reason=budget"
                    printed = io.print_line(line)
                    lines.append(line)
                    delivery = ModelItemDelivery(
                        digest=item.digest,
                        kind="queue",
                        printed=printed,
                        tick_id=snapshot.tick_id,
                        position=position,
                        reason="budget",
                    )
                    queued.append((item.item_id, delivery))
                else:
                    delivery = ModelItemDelivery(
                        digest=item.digest,
                        kind="question",
                        accepted=receipt.accepted,
                        printed=receipt.printed,
                    )
                if plan.escalation or acquired:
                    store.notice(item.family, delivery)
                items[index] = item.model_copy(
                    update={"deliveries": (*item.deliveries, delivery)}
                )
                line = f"{item.item_id} asked cause={plan.cause}"
            io.print_line(line)
            lines.append(line)
        snapshot = snapshot.model_copy(update={"items": tuple(items)})
        return ModelItemTickResult(
            findings=findings,
            started=tuple(started),
            queued=tuple(queued),
            lines=tuple(lines),
            snapshot=snapshot,
            g2_unanswered=g2_unanswered(snapshot, self.policy),
        )
