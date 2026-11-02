# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the launch validity tests: invented lanes, tickets and PRs, and an in-memory bus."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from omnibase_infra.event_bus.models.model_event_headers import ModelEventHeaders
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_launch_validity_compute.models import (
    EnumRead,
    ModelLaunchTarget,
    ModelLaunchValidityPolicy,
    ModelLaunchValidityRequest,
    ModelStateRead,
)
from omnimarket.nodes.node_launch_validity_compute.overlay import (
    load_launch_validity_policy,
)

HEAD = "0123456789abcdef0123456789abcdef01234567"
OTHER_HEAD = "fedcba9876543210fedcba9876543210fedcba98"
PR = "example-org/example-repo#12"


def policy(environ: dict[str, str] | None = None) -> ModelLaunchValidityPolicy:
    return load_launch_validity_policy(environ if environ is not None else {})


def fresh(**fields: Any) -> ModelStateRead:
    return ModelStateRead(ok=True, age_seconds=0, **fields)


def good_reads() -> dict[EnumRead, ModelStateRead]:
    return {
        EnumRead.PR: fresh(state="OPEN", head=HEAD, pr_class="green"),
        EnumRead.TICKET: fresh(state="In Progress"),
        EnumRead.PLAN_TASK: fresh(),
        EnumRead.OWNER: fresh(),
    }


def request(
    reads: dict[EnumRead, ModelStateRead] | None = None,
    *,
    target: ModelLaunchTarget | None = None,
    environ: dict[str, str] | None = None,
) -> ModelLaunchValidityRequest:
    return ModelLaunchValidityRequest(
        target=target
        or ModelLaunchTarget(
            lane="lane-a",
            ticket="EX-1",
            plan_task="plan-1/task-3",
            pr=PR,
            expected_head=HEAD,
        ),
        reads=good_reads() if reads is None else reads,
        policy=policy(environ),
    )


class InMemoryBus:
    """The in-memory overlay's bus: a topic is a list of subscribers, publishing calls them in order."""

    def __init__(self) -> None:
        self.subscribers: dict[
            str, list[Callable[[ModelEventMessage], Awaitable[None]]]
        ] = {}

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> object:
        message = ModelEventMessage(
            topic=topic,
            key=key,
            value=value,
            headers=ModelEventHeaders(
                source="launch-validity-test",
                event_type=topic,
                timestamp=datetime.now(UTC),
            ),
        )
        for callback in list(self.subscribers.get(topic, [])):
            await callback(message)
        return None

    async def subscribe_ungrouped(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
    ) -> Callable[[], Awaitable[None]]:
        self.subscribers.setdefault(topic, []).append(on_message)

        async def unsubscribe() -> None:
            self.subscribers[topic].remove(on_message)

        return unsubscribe
