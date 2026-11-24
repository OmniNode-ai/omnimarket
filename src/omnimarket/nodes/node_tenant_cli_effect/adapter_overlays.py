# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay documents a minted key depends on, read as files: the gateway's key prefix and the key node's lane.

Only the named fields are read, never the other node's model, so this node imports nothing private of either.
"""

from __future__ import annotations

import yaml

from omnimarket.handlers.bus_binding.node_event_bus_overlay import node_overlay_path
from omnimarket.nodes.node_tenant_cli_effect.handlers.contract_topics import KEY_NODE
from omnimarket.nodes.node_tenant_cli_effect.ports.protocol_tenant_cli import (
    TenantOverlayUnavailableError,
)

GATEWAY_NODE = "node_workflow_gateway_effect"


def _document(node_id: str) -> dict[str, object]:
    path = node_overlay_path(node_id, None)
    if path is None or not path.is_file():
        raise TenantOverlayUnavailableError(f"{node_id} has no overlay.yaml here")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise TenantOverlayUnavailableError(
            f"the overlay of {node_id} is unreadable ({type(exc).__name__})"
        ) from None
    if not isinstance(raw, dict):
        raise TenantOverlayUnavailableError(
            f"the overlay of {node_id} is not a mapping"
        )
    return raw


class FileGatewayKeyOverlays:
    """``ProtocolGatewayKeyOverlays`` over the two nodes' overlay files."""

    def gateway_prefix(self) -> str:
        api_key = _document(GATEWAY_NODE).get("api_key")
        prefix = api_key.get("prefix") if isinstance(api_key, dict) else None
        if not isinstance(prefix, str) or not prefix:
            raise TenantOverlayUnavailableError(
                f"the overlay of {GATEWAY_NODE} declares no api_key.prefix"
            )
        return prefix

    def key_node_lane(self) -> str | None:
        lane = _document(KEY_NODE).get("lane")
        return lane if isinstance(lane, str) and lane else None

    def key_node_dev_store_environment(self) -> str | None:
        environment = _document(KEY_NODE).get("dev_secret_store_environment")
        return environment if isinstance(environment, str) and environment else None


__all__: list[str] = ["GATEWAY_NODE", "FileGatewayKeyOverlays"]
