# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure plan handler: one local-identity-minted event and the tenant's stored row in, the row out.

It calls the shared ``plan_local_identity_write`` in ``omnimarket.models.model_local_identity_fold``,
so the local SQLite path and the Postgres writer fold identically. It reads no clock beyond the
write stamp and keeps no state.
"""

from omnimarket.models.model_local_identity_fold import plan_local_identity_write
from omnimarket.nodes.node_projection_local_identity.models.model_local_identity_plan import (
    ModelLocalIdentityPlanRequest,
    ModelLocalIdentityPlanResponse,
)


class HandlerProjectionLocalIdentity:
    def handle(
        self, request: ModelLocalIdentityPlanRequest
    ) -> ModelLocalIdentityPlanResponse:
        row = plan_local_identity_write(
            request.event, request.stored_rows, request.updated_at
        )
        return ModelLocalIdentityPlanResponse(row=row)


__all__ = ["HandlerProjectionLocalIdentity"]
