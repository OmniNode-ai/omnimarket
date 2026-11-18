# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_consumer_group_reap_effect: the broker's groups, their deletion and its health."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class GroupLister(Protocol):
    def __call__(self, config: Mapping[str, str]) -> dict[str, str]:
        """Every consumer group on the broker, name -> upper-case state such as EMPTY, STABLE."""
        ...


class GroupDeleter(Protocol):
    def __call__(self, config: Mapping[str, str], names: list[str]) -> dict[str, str]:
        """Delete the named groups; the value is "" on success or the error name."""
        ...


class BrokerHealth(Protocol):
    def __call__(self, config: Mapping[str, str]) -> str:
        """ "" when the broker is healthy, else the reason it is not."""
        ...


__all__ = [
    "BrokerHealth",
    "GroupDeleter",
    "GroupLister",
]
