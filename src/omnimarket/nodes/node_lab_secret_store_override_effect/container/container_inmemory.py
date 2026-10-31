# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the in-memory transports the local render command is published over (OMN-21695).

The command never constructs a broker or transport itself: it takes the ones this
module binds, in the groups its node contract's ``in_memory_bindings`` section names.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from omnibase_core.runtime.transport import InMemoryBroker, InMemoryTransport

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)

_NODE = "node_lab_secret_store_override_effect"


def _group(key: str) -> str:
    declared = cast(
        dict[str, str], load_packaged_contract_section(_NODE, "in_memory_bindings")
    )
    return declared[key]


def bind_broker() -> InMemoryBroker:
    """Bind a fresh in-memory broker."""
    return InMemoryBroker()


def bind_consumer(
    broker: InMemoryBroker, *, group: str, topics: Sequence[str]
) -> InMemoryTransport:
    """Bind the transport the runtime consumes commands from, in the caller's group."""
    return InMemoryTransport(broker=broker, group=group, topics=topics)


def bind_publisher(broker: InMemoryBroker) -> InMemoryTransport:
    """Bind the transport the command is published over, in the contract's publisher group."""
    return InMemoryTransport(broker=broker, group=_group("publisher_group"))


def bind_terminal_reader(
    broker: InMemoryBroker, *, topics: Sequence[str]
) -> InMemoryTransport:
    """Bind the transport the terminal event is read from, in the contract's terminal group."""
    return InMemoryTransport(
        broker=broker, group=_group("terminal_group"), topics=topics
    )
