# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployment overlay that tells a catalog mount where its secrets are resolved from."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCatalogDatabaseOverlay(BaseModel):
    """Overlay document of a Postgres-bound catalog node.

    The database address is a secret and is never carried here: the overlay names
    the runtime secret resolver configuration, and the node asks that resolver for
    the logical name its contract declares.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    secret_resolver_config_path: str = Field(
        ...,
        min_length=1,
        description="Path of the rendered secret resolver configuration of the lane.",
    )


__all__: list[str] = ["ModelCatalogDatabaseOverlay"]
