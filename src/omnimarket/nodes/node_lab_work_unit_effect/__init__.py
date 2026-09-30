# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_work_unit_effect -- heavy work placed on a pool host and sent over
the bus (OMN-20105).

Each pool host runs ``onex lab-work serve``: it advertises its capacity on a
fixed cadence (``HandlerHostCapacityAdvertiseEffect``) and runs the work units
addressed to it (``HandlerLabWorkUnitEffect``). ``onex lab-work run`` reads the
advertisements, places a unit on the host with the most free capacity, sends
it on the command topic and waits for its receipt.
"""

from omnimarket.nodes.node_lab_work_unit_effect.handlers import (
    DEFAULT_ALLOWED_EXECUTABLES,
    DEFAULT_ALLOWED_OWNERS,
    HandlerHostCapacityAdvertiseEffect,
    HandlerLabWorkUnitEffect,
)
from omnimarket.nodes.node_lab_work_unit_effect.models import (
    EnumLabWorkUnitStatus,
    ModelHostCapacityAdvertisement,
    ModelHostCapacityProbeRequest,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)


class NodeLabWorkUnitEffect(HandlerLabWorkUnitEffect):
    """ONEX entry-point wrapper for HandlerLabWorkUnitEffect."""


__all__ = [
    "DEFAULT_ALLOWED_EXECUTABLES",
    "DEFAULT_ALLOWED_OWNERS",
    "EnumLabWorkUnitStatus",
    "HandlerHostCapacityAdvertiseEffect",
    "HandlerLabWorkUnitEffect",
    "ModelHostCapacityAdvertisement",
    "ModelHostCapacityProbeRequest",
    "ModelLabWorkUnitReceipt",
    "ModelLabWorkUnitRequest",
    "NodeLabWorkUnitEffect",
]
