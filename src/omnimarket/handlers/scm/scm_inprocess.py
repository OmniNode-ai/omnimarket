# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host the selected SCM contract on the CLI's private in-memory bus.

Use the runtime's discovery, handler resolution, dispatch and result publishing
path. Only the requested contract is loaded; unrelated runtime bootstrap
configuration is neither needed nor read.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from importlib import resources

from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    subscribe_wired_contract_topics,
    wire_from_manifest,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.models.scm import EnumScmNode


@asynccontextmanager
async def open_scm_inprocess(
    opened: AbstractAsyncContextManager[ProtocolLabRunBus], node: EnumScmNode
) -> AsyncIterator[ProtocolLabRunBus]:
    """Wire before publishing; closing the private bus removes its subscriptions."""
    resource = resources.files(f"omnimarket.nodes.{node.value}").joinpath(
        "contract.yaml"
    )
    with resources.as_file(resource) as path:
        manifest = discover_contracts_from_paths([path])
        if manifest.errors or len(manifest.contracts) != 1:
            raise LabRunBusError(f"cannot discover {node.value}: {manifest.errors}")
        async with opened as bus:
            engine = MessageDispatchEngine()
            report = await wire_from_manifest(
                manifest,
                engine,
                event_bus=bus,
                environment="local",
                subscribe_immediately=False,
            )
            engine.freeze()
            attached = await subscribe_wired_contract_topics(
                manifest, report, engine, bus, environment="local"
            )
            if not attached.get(node.value):
                raise LabRunBusError(f"no in-memory consumer attached for {node.value}")
            yield bus
