# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_entry_point_registration_compute: the one reviewed list of nodes allowed without an entry point (OMN-24354)."""

from collections.abc import Collection
from functools import cache

from omnimarket.nodes.node_entry_point_registration_compute.handlers.handler_entry_point_registration import (
    HandlerEntryPointRegistration,
)
from omnimarket.nodes.node_entry_point_registration_compute.models import (
    ModelEntryPointRegistrationRequest,
)


class NodeEntryPointRegistrationCompute(HandlerEntryPointRegistration):
    """ONEX entry-point wrapper for HandlerEntryPointRegistration."""


@cache
def registration_handler() -> HandlerEntryPointRegistration:
    return HandlerEntryPointRegistration()


def is_registered_or_reviewed(node_name: str, entry_points: Collection[str]) -> bool:
    """True when the node has an onex.nodes entry point or is on the reviewed list."""
    return (
        registration_handler()
        .handle(
            ModelEntryPointRegistrationRequest(
                node_name=node_name, entry_points=frozenset(entry_points)
            )
        )
        .passes
    )


__all__ = [
    "HandlerEntryPointRegistration",
    "NodeEntryPointRegistrationCompute",
    "is_registered_or_reviewed",
]
