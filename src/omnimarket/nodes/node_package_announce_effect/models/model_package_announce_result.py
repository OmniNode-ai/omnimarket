# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Result of the announce step: what the announcer did and what the catalog holds (OMN-21597)."""

from omnibase_core.models.nodes.package_announce.model_catalog_entry import (
    ModelCatalogEntry,
)
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_package_announce_effect.models.model_announce_report import (
    ModelAnnounceReport,
)


class ModelPackageAnnounceResult(BaseModel):
    """The announcer's report and a snapshot of the catalog built from its announcements."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report: ModelAnnounceReport
    entries: dict[str, ModelCatalogEntry] = Field(default_factory=dict)
