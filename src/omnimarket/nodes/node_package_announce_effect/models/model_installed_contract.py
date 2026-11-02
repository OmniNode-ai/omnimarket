# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""One contract an installed distribution lists in its own metadata (OMN-21597)."""

from pathlib import Path

from omnibase_core.enums.enum_announced_node_kind import EnumAnnouncedNodeKind
from omnibase_core.models.events.contract_registration.model_announced_package import (
    ModelAnnouncedPackage,
)
from pydantic import BaseModel, ConfigDict, Field


class ModelInstalledContract(BaseModel):
    """A contract named by a distribution's metadata, located without importing it.

    ``contract_path`` is ``None`` when the metadata names a contract the
    distribution does not hold, which the announcer refuses with a typed reason.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    listing_name: str = Field(min_length=1, description="Name in the package metadata")
    package: ModelAnnouncedPackage
    node_kind: EnumAnnouncedNodeKind
    contract_path: Path | None = None
