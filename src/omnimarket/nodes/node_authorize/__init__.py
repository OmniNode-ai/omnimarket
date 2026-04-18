# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_authorize — write ModelAgentAuthorizationGrant for /onex:authorize skill.

Backs the `/onex:authorize` Claude Code skill. Task 3 of the unused-hooks
plan (OMN-9087 PermissionRequest authorization gate) reads the file this
node writes.
"""

from omnimarket.nodes.node_authorize.handlers.handler_authorize import (
    AuthorizeRequest,
    AuthorizeResult,
    HandlerAuthorize,
)
from omnimarket.nodes.node_authorize.models.model_agent_authorization_grant import (
    AUTHORIZATION_FILE_RELATIVE_PATH,
    ModelAgentAuthorizationGrant,
    load_grant_if_valid,
)

__all__ = [
    "AUTHORIZATION_FILE_RELATIVE_PATH",
    "AuthorizeRequest",
    "AuthorizeResult",
    "HandlerAuthorize",
    "ModelAgentAuthorizationGrant",
    "load_grant_if_valid",
]
