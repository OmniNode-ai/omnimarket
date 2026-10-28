# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which stub backs which binding id for each protocol code a parity run resolves.

A binding id is a value of the ``factory_bindings`` overlay. The ids here name stub
implementations only; the overlay says which profile uses which.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from omnimarket.nodes.node_binding_parity_orchestrator.protocols.protocol_local_bus import (
    ProtocolLocalBus,
)
from omnimarket.nodes.node_binding_parity_orchestrator.stubs.stub_bus import (
    StubBusDropsLast,
    StubBusDuplicate,
    StubBusOutOfOrder,
    build_reference_bus,
)
from omnimarket.nodes.node_binding_parity_orchestrator.stubs.stub_clock import (
    StubClockBackward,
    StubClockFixed,
)
from omnimarket.protocols.protocol_clock import ProtocolClock

PROTOCOL_BUS = "BUS"
PROTOCOL_CLOCK = "CLK"

BUS_FACTORIES: dict[str, Callable[[], ProtocolLocalBus]] = {
    "reference": build_reference_bus,
    "out_of_order": StubBusOutOfOrder,
    "duplicate": StubBusDuplicate,
    "drops_row": StubBusDropsLast,
}

CLOCK_FACTORIES: dict[str, Callable[[datetime], ProtocolClock]] = {
    "fixed": StubClockFixed,
    "backward": StubClockBackward,
}
