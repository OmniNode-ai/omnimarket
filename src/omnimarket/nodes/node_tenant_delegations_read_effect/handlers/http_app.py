# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ASGI factory loaded by the runtime's contract-owned HTTP mount."""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import FastAPI

from omnimarket.handlers.container.container_tenant_read_dependencies import (
    bound_identity,
    bound_reader,
)
from omnimarket.handlers.tenant_projection_read.http_gate import build_app, load_route
from omnimarket.nodes.node_tenant_delegations_read_effect.handlers.handler_tenant_delegations_read import (
    HandlerTenantDelegationsRead,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)
from omnimarket.protocols.protocol_tenant_delegation_projection_reader import (
    ProtocolTenantDelegationProjectionReader,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


def create_app(
    *,
    identity: ProtocolHttpTenantIdentity | None = None,
    reader: ProtocolTenantDelegationProjectionReader | None = None,
) -> FastAPI:
    """Build the mount without connecting to a database or resolving a secret.

    Dependencies come from the installed lane overlay; an argument replaces the
    overlay's binding for that protocol (tests). With no identity bound the app
    refuses every request.
    """
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    route = load_route(CONTRACT_PATH)
    if (route.handler.module, route.handler.name) != (
        HandlerTenantDelegationsRead.__module__,
        HandlerTenantDelegationsRead.__name__,
    ):
        raise ValueError("HTTP handler binding does not match the mount factory")
    secret = str(contract["config"]["analytics_dsn_secret"])
    handler = HandlerTenantDelegationsRead(reader=reader or bound_reader(secret))
    return build_app(
        route=route, identity=identity or bound_identity(), handle=handler.handle
    )
