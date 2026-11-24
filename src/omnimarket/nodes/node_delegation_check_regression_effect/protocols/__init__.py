# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports of the delegation check regression alarm: where results and changes are read, and where outputs go."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel

from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    ModelChange,
    ModelProbeRun,
    ModelRebuildCompletedObserved,
    ModelWatchedCheck,
)


class ProbeRunsUnreadableError(Exception):
    """A check's finished runs could not be read; the check is reported unread, never counted green or red."""


class ProtocolProbeRunSource(Protocol):
    def runs(self, check: ModelWatchedCheck, limit: int) -> tuple[ModelProbeRun, ...]:
        """The check's finished runs (pass or fail only), in any order; raises ProbeRunsUnreadableError."""
        ...


class ProtocolChangeSource(Protocol):
    def between(self, start: datetime, end: datetime) -> tuple[ModelChange, ...]:
        """Merges, dev builds and host or model setting changes with ``start < at <= end``."""
        ...


class ProtocolLiveClaims(Protocol):
    def holder(self, check_id: str) -> str | None:
        """The lane of a live CLAIM that holds this check's regression, or None."""
        ...


class ProtocolSeenStore(Protocol):
    def seen(self, event_id: str) -> bool: ...

    def record(self, event_id: str) -> None: ...


class ProtocolRegressionPublisher(Protocol):
    def publish(self, topic: str, key: str, payload: BaseModel) -> None:
        """Publish one typed payload to a contract-declared topic, or raise OSError."""
        ...


class ProtocolDevBuildStore(Protocol):
    def append(self, build: ModelRebuildCompletedObserved) -> None:
        """Keep one completed rebuild, or raise OSError."""
        ...
