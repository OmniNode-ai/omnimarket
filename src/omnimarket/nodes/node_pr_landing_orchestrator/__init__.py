# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR landing workflow orchestrator (wave-1 seam: contract and models only).

The handler, the bus declarations and the ``state_io`` codec land in wave 2.
Until then the contract declares no ``handler_routing`` and no ``event_bus``,
so runtime auto-wiring discovers this node and wires nothing for it.
"""
