# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""What the announcer asks the catalog for after it publishes (OMN-21597)."""

from typing import Protocol, runtime_checkable

from omnibase_core.models.nodes.package_announce.model_catalog_outcome import (
    ModelCatalogOutcome,
)


@runtime_checkable
class ProtocolAnnounceOutcomeSource(Protocol):
    """The catalog's answer for one name and generation, or ``None`` before it answers."""

    def outcome_for(self, name: str, generation: int) -> ModelCatalogOutcome | None:
        """Return what the catalog did with the announcement of this name and generation."""
        ...
