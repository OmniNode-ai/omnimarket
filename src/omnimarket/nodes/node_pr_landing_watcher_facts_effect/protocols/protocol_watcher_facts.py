# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the landing watcher facts node (OMN-23507)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from omnimarket.models.landing_decision import ModelLandingFacts


@runtime_checkable
class ProtocolWatcherStateSource(Protocol):
    """The PR watcher's state, read once per pass."""

    def read_state(self) -> object: ...


@runtime_checkable
class ProtocolLandingFactsSink(Protocol):
    """Where the facts go; returns where they were written."""

    def write(self, facts: ModelLandingFacts) -> str: ...


@runtime_checkable
class ProtocolWatcherFactsPublisher(Protocol):
    """Publishes the written or failed event on its topic."""

    def publish(self, topic: str, event: BaseModel) -> None: ...


__all__: list[str] = [
    "ProtocolLandingFactsSink",
    "ProtocolWatcherFactsPublisher",
    "ProtocolWatcherStateSource",
]
