# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Observed host installation status judged against a desired generation."""

from enum import StrEnum

from pydantic import Field

from omnimarket.models.model_host_wire import (
    HostSurface,
    ModelHostWire,
    NonEmptyStr,
    UtcStamp,
)


class EnumHostObservedStatus(StrEnum):
    """The result of judging a host surface against desired state."""

    CURRENT = "current"
    LAGGING = "lagging"
    FAILED = "failed"
    REFUSED = "refused"
    UNREADABLE = "unreadable"
    ABSENT = "absent"


class ModelHostObservedState(ModelHostWire):
    """One host surface's installed and loaded revisions at observation time."""

    host: NonEmptyStr
    surface: HostSurface
    generation: int = Field(ge=0)
    sequence: int = Field(ge=0)
    observed_at: UtcStamp
    status: EnumHostObservedStatus
    installed_revision: str | None = None
    loaded_revision: str | None = None
    detail: str = ""

    @property
    def key(self) -> str:
        """Address the observed host surface."""
        return f"{self.host}/{self.surface}"

    @property
    def fold_key(self) -> tuple[int, int]:
        """Order observations by desired generation and then sequence."""
        return self.generation, self.sequence
