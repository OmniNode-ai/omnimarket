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
from omnimarket.handlers.edge_overlay_identity import OverlayBoundTenantIdentity
from omnimarket.handlers.tenant_projection_read.http_gate import build_app, load_route
from omnimarket.nodes.node_tenant_savings_read_effect.handlers.handler_tenant_savings_read import (
    HandlerTenantSavingsRead,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)
from omnimarket.protocols.protocol_tenant_delegation_projection_reader import (
    ProtocolTenantDelegationProjectionReader,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

OVERLAY_ID = "node_tenant_savings_read_effect"


def create_app(
    *,
    identity: ProtocolHttpTenantIdentity | None = None,
    reader: ProtocolTenantDelegationProjectionReader | None = None,
) -> FastAPI:
    """Build the mount without connecting to a database or resolving a secret.

    Dependencies come from the installed lane overlay; an argument replaces the
    overlay's binding for that protocol (tests). With no identity bound by
    argument or lane overlay, the node's own overlay document supplies the
    resolver; with none, every request is refused.
    """
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    route = load_route(CONTRACT_PATH)
    if (route.handler.module, route.handler.name) != (
        HandlerTenantSavingsRead.__module__,
        HandlerTenantSavingsRead.__name__,
    ):
        raise ValueError("HTTP handler binding does not match the mount factory")
    secret = str(contract["config"]["analytics_dsn_secret"])
    uncounted = list(contract["config"]["uncounted_data_sources"])
    handler = HandlerTenantSavingsRead(reader=reader or bound_reader(secret, uncounted))
    return build_app(
        route=route,
        identity=identity or bound_identity() or OverlayBoundTenantIdentity(OVERLAY_ID),
        handle=handler.handle,
    )
