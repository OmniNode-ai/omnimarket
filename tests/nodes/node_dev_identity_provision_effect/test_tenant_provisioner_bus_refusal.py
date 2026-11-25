# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A broker that refuses the tenant provision read is a typed outcome, not a traceback (OMN-23563).

Measured on the dev lane from a lab host whose broker login does not hold READ on the tenant provision completed topic:
the group-less subscribe raised ``InfraConnectionError`` (TopicAuthorizationFailedError) out of the bus adapter, which
only translated ``LabRunBusError`` and ``OSError``, so the run ended in a traceback with no result line instead of the
``runtime_unreachable`` outcome the node contract promises for every failure.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import uuid4

import pytest
from omnibase_infra.errors import InfraConnectionError

from omnimarket.nodes.node_dev_identity_provision_effect.adapters import (
    BusTenantProvisioner,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_tenant_provision_wire import (
    ModelTenantProvisionRequest,
    ModelTenantProvisionTopics,
)
from omnimarket.nodes.node_dev_identity_provision_effect.protocols import (
    TenantProvisionUnavailableError,
)

pytestmark = pytest.mark.unit

TOPICS = ModelTenantProvisionTopics(
    command_topic="onex.cmd.omnimarket.tenant-provision-requested.v1",
    completed_topic="onex.evt.omnimarket.tenant-provision-completed.v1",
    wait_seconds=0.5,
)


class _RefusingBus:
    """A bus whose broker refuses the group-less read, as the dev broker does for a login without the grant."""

    def __init__(self) -> None:
        self.published: list[str] = []

    async def subscribe_ungrouped(
        self, topic: str, on_message: Callable[[Any], Awaitable[None]]
    ) -> Callable[[], Awaitable[None]]:
        raise InfraConnectionError(
            f"Failed to start group-less read of topic {topic}: "
            f"[Error 29] TopicAuthorizationFailedError: {topic}"
        )

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> object:
        self.published.append(topic)
        return None


def test_a_broker_refusing_the_answer_read_is_runtime_unreachable_and_nothing_is_sent() -> (
    None
):
    bus = _RefusingBus()

    @contextlib.asynccontextmanager
    async def open_bus() -> AsyncIterator[_RefusingBus]:
        yield bus

    provisioner = BusTenantProvisioner(open_bus=open_bus, topics=TOPICS)
    request = ModelTenantProvisionRequest(
        command_id=uuid4(), tenant_slug="dev-probe-user", name="dev-probe-user"
    )

    with pytest.raises(
        TenantProvisionUnavailableError, match="TopicAuthorizationFailedError"
    ):
        asyncio.run(provisioner.provision(request))

    assert bus.published == []
