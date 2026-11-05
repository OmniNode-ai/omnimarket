# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay-bound read, journal and event ports; none can write credentials."""

from typing import Protocol, runtime_checkable

from omnimarket.models.host_checks import (
    ModelHostCheckEvent,
    ModelHostCheckPolicy,
    ModelHostCheckSnapshot,
    ModelHostCheckTickResult,
)


@runtime_checkable
class ProtocolHostCheckFacts(Protocol):
    def read(self, policy: ModelHostCheckPolicy) -> ModelHostCheckSnapshot: ...


@runtime_checkable
class ProtocolHostCheckJournal(Protocol):
    def result(self, tick_id: str) -> ModelHostCheckTickResult | None: ...

    def record(
        self, result: ModelHostCheckTickResult, events: tuple[ModelHostCheckEvent, ...]
    ) -> ModelHostCheckTickResult: ...

    def pending(self) -> tuple[ModelHostCheckEvent, ...]: ...

    def delivered(self, key: str) -> None: ...


@runtime_checkable
class ProtocolHostCheckEvents(Protocol):
    @property
    def principal(self) -> str: ...

    def publish(self, event: ModelHostCheckEvent) -> None: ...
