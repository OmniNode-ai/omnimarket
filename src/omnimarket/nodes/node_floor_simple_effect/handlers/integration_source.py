# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The integration projection as the floor cycle reads it from the projection bundle (OMN-23899).

The bundle is what node_floor_projection_read_effect wrote off the bus; the age is judged against the projection's own
bound by ``projection_payload``. A payload that is not an integration projection reads unread with the reason, never as
an empty list.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError

from omnimarket.handlers.floor_projection import projection_payload
from omnimarket.models.floor_17a.model_floor_integration import ModelFloorIntegrationIn
from omnimarket.models.floor_projection import ModelFloorProjectionBundle
from omnimarket.models.integration_reading import ModelIntegrationProjection


def read_integration(
    bundle: ModelFloorProjectionBundle | None, projection_id: str, now: datetime
) -> ModelFloorIntegrationIn:
    """The projection, read; or unread with why (no bundle, not declared, unread, older than its bound, not the shape)."""
    found = projection_payload(bundle, projection_id, now)
    if found.payload is None:
        return ModelFloorIntegrationIn(
            status="unread", reason=found.reason, as_of=found.as_of, age_s=found.age_s
        )
    try:
        projection = ModelIntegrationProjection.model_validate(found.payload)
    except ValidationError as exc:
        return ModelFloorIntegrationIn(
            status="unread",
            reason=f"the {projection_id} projection is not an integration projection ({exc.error_count()} errors)",
            as_of=found.as_of,
            age_s=found.age_s,
        )
    return ModelFloorIntegrationIn(
        status="read", as_of=found.as_of, age_s=found.age_s, projection=projection
    )


__all__ = ["read_integration"]
