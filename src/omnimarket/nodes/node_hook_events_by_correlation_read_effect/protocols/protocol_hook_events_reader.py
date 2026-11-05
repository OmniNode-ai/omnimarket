# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The repository the by-correlation read goes through.

The in-memory profile binds a fake; the deployed runtime binds the database
adapter whose settings come from the projection read binding overlay.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol


class ProjectionAbsentError(RuntimeError):
    """The hook_events projection table does not exist on this plane yet."""


class ProjectionNotConfiguredError(RuntimeError):
    """No projection database binding is configured for this runtime."""


class ProjectionUnavailableError(RuntimeError):
    """The projection database is configured and unreachable or errored."""


class ProtocolHookEventsReader(Protocol):
    async def read(
        self, *, tenant_slug: str, correlation_id: str, limit: int
    ) -> Sequence[Mapping[str, Any]]:
        """Return this tenant's rows for the correlation id, newest first.

        Raises ProjectionAbsentError, ProjectionNotConfiguredError or
        ProjectionUnavailableError; never returns an empty list for a failure.
        """
        ...


__all__: list[str] = [
    "ProjectionAbsentError",
    "ProjectionNotConfiguredError",
    "ProjectionUnavailableError",
    "ProtocolHookEventsReader",
]
