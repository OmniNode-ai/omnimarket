# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerStackPlan: the pure install/remove plan for a declared stability stack (OMN-23968).

Pure and deterministic: no clock, no file, no network, no shell. Everything the
plan needs comes from the frozen overlay (the stack descriptions and the
reserved lanes of the other stacks) and the request (stack name, action).

The plan is refused, naming what is wrong, when:

* the request names a stack the overlay does not declare (``unknown_stack``);
* the stack's ``release_tag`` does not fullmatch the overlay's
  ``tag_pattern``, which admits only dev-merge tags (``bad_release_tag``);
* the stack collides with a reserved lane or with another declared stack:
  the same ``compose_project`` (any host), a container name or a volume
  (whatever the host — names are global), or a published port on the same
  host. Each collision is one refusal naming the subject and the lane or
  stack it collides with; refusals are sorted deterministically.

Any refusal means no steps at all: nothing may start. Otherwise an install
plan reads render, snapshot, one ``up`` per tier in bring-up order, one
``read_services``, one ``read_health`` per declared health read and one
``event_roundtrip``, with rollback ``down`` then ``read_gone``; a remove plan
is ``down`` then ``read_gone`` with an empty rollback. The same input gives
byte-equal output on every call, and ``render_digest`` is the sha256 hex of
the canonical sorted-key JSON of the stack description.
"""

from __future__ import annotations

import hashlib
import json
import re

from omnimarket.nodes.node_stability_stack_plan_compute.models.model_stack_plan import (
    EnumStackRefusalKind,
    EnumStackStepKind,
    ModelStackDescription,
    ModelStackPlan,
    ModelStackPlanOverlay,
    ModelStackPlanRequest,
    ModelStackRefusal,
    ModelStackStep,
)

_Other = tuple[
    str,  # name
    str,  # host
    str,  # compose_project
    tuple[str, ...],  # container_names
    tuple[str, ...],  # volumes
    tuple[int, ...],  # published ports
]


def _teardown_steps() -> tuple[ModelStackStep, ModelStackStep]:
    """Fresh ``down`` then ``read_gone`` steps, so no plan shares a mutable step."""
    return (
        ModelStackStep(index=0, kind=EnumStackStepKind.DOWN),
        ModelStackStep(index=1, kind=EnumStackStepKind.READ_GONE),
    )


def _render_digest(stack: ModelStackDescription) -> str:
    """The sha256 hex of the canonical sorted-key JSON of the stack description."""
    payload = json.dumps(
        stack.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _refusal_key(refusal: ModelStackRefusal) -> tuple[str, str, str]:
    return (refusal.kind.value, refusal.subject, refusal.against)


class HandlerStackPlan:
    """Fold the declared stack, the reserved lanes and the request into a plan."""

    def __init__(self, overlay: ModelStackPlanOverlay) -> None:
        self._overlay = overlay

    def handle(self, request: ModelStackPlanRequest) -> ModelStackPlan:
        stack = next((s for s in self._overlay.stacks if s.name == request.stack), None)
        if stack is None:
            return ModelStackPlan(
                status="refused",
                stack=request.stack,
                host="",
                compose_project="",
                action=request.action,
                steps=(),
                rollback_steps=(),
                refusals=(
                    ModelStackRefusal(
                        kind=EnumStackRefusalKind.UNKNOWN_STACK,
                        subject=request.stack,
                        against="",
                    ),
                ),
                render_digest="",
            )

        digest = _render_digest(stack)
        refusals = self._collision_refusals(stack)
        if re.fullmatch(self._overlay.tag_pattern, stack.release_tag) is None:
            refusals.append(
                ModelStackRefusal(
                    kind=EnumStackRefusalKind.BAD_RELEASE_TAG,
                    subject=stack.release_tag,
                    against=self._overlay.tag_pattern,
                )
            )
        if refusals:
            return ModelStackPlan(
                status="refused",
                stack=stack.name,
                host=stack.host,
                compose_project=stack.compose_project,
                action=request.action,
                steps=(),
                rollback_steps=(),
                refusals=tuple(sorted(refusals, key=_refusal_key)),
                render_digest=digest,
            )

        if request.action == "remove":
            return ModelStackPlan(
                status="planned",
                stack=stack.name,
                host=stack.host,
                compose_project=stack.compose_project,
                action=request.action,
                steps=_teardown_steps(),
                rollback_steps=(),
                refusals=(),
                render_digest=digest,
            )

        steps: list[ModelStackStep] = [
            ModelStackStep(index=0, kind=EnumStackStepKind.RENDER),
            ModelStackStep(index=1, kind=EnumStackStepKind.SNAPSHOT),
        ]
        index = 2
        for tier in stack.tiers:
            steps.append(
                ModelStackStep(
                    index=index,
                    kind=EnumStackStepKind.UP,
                    tier=tier.name,
                    target=" ".join(tier.services),
                )
            )
            index += 1
        steps.append(ModelStackStep(index=index, kind=EnumStackStepKind.READ_SERVICES))
        index += 1
        for read in stack.health_reads:
            steps.append(
                ModelStackStep(
                    index=index,
                    kind=EnumStackStepKind.READ_HEALTH,
                    target=read.container,
                )
            )
            index += 1
        steps.append(
            ModelStackStep(
                index=index,
                kind=EnumStackStepKind.EVENT_ROUNDTRIP,
                target=stack.event_roundtrip.broker_container,
            )
        )
        return ModelStackPlan(
            status="planned",
            stack=stack.name,
            host=stack.host,
            compose_project=stack.compose_project,
            action=request.action,
            steps=tuple(steps),
            rollback_steps=_teardown_steps(),
            refusals=(),
            render_digest=digest,
        )

    def _collision_refusals(
        self, stack: ModelStackDescription
    ) -> list[ModelStackRefusal]:
        """One refusal per collision of ``stack`` with a reserved lane or another stack.

        Compose projects, container names and volumes collide whatever the host
        (names and projects are global); a published port collides only when the
        other lane or stack sits on the same host.
        """
        others: list[_Other] = [
            (
                lane.name,
                lane.host,
                lane.compose_project,
                lane.container_names,
                lane.volumes,
                lane.ports,
            )
            for lane in self._overlay.reserved
        ]
        others.extend(
            (
                other.name,
                other.host,
                other.compose_project,
                other.container_names,
                other.volumes,
                tuple(port for _, port in other.ports),
            )
            for other in self._overlay.stacks
            if other.name != stack.name
        )
        my_ports = tuple(port for _, port in stack.ports)
        found: set[tuple[EnumStackRefusalKind, str, str]] = set()
        for name, host, project, containers, volumes, ports in others:
            if project == stack.compose_project:
                found.add(
                    (
                        EnumStackRefusalKind.PROJECT_COLLISION,
                        stack.compose_project,
                        name,
                    )
                )
            if host == stack.host:
                for port in my_ports:
                    if port in ports:
                        found.add(
                            (EnumStackRefusalKind.PORT_COLLISION, str(port), name)
                        )
            for container in stack.container_names:
                if container in containers:
                    found.add(
                        (EnumStackRefusalKind.CONTAINER_COLLISION, container, name)
                    )
            for volume in stack.volumes:
                if volume in volumes:
                    found.add((EnumStackRefusalKind.VOLUME_COLLISION, volume, name))
        return [
            ModelStackRefusal(kind=kind, subject=subject, against=against)
            for kind, subject, against in sorted(
                found, key=lambda item: (item[0].value, item[1], item[2])
            )
        ]


__all__ = ["HandlerStackPlan"]
