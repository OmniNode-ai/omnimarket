# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The last GitHub quota reading per resource, held for a handler (OMN-21536).

A GitHub effect handler refuses a call when the last reading it saw for the
call's resource is under the contract floor. The readings are state, so they live
here, in a collaborator the handler is given, and not on the handler: a public
handler method never assigns to the handler.
"""

from __future__ import annotations

from omnimarket.events.pr_landing_github.model_github_quota_reading import (
    ModelGithubQuotaReading,
)

__all__: list[str] = ["GithubQuotaReadings"]


class GithubQuotaReadings:
    """The newest quota reading seen for each rate-limit resource."""

    def __init__(self) -> None:
        self._by_resource: dict[str, ModelGithubQuotaReading] = {}

    def record(self, reading: ModelGithubQuotaReading) -> None:
        """Keep ``reading`` as the last one for its resource."""
        self._by_resource[reading.resource] = reading

    def last(self, resource: str) -> ModelGithubQuotaReading | None:
        """The last reading recorded for ``resource``, or None when none was."""
        return self._by_resource.get(resource)
