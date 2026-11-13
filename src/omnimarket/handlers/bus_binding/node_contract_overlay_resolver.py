# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22472: Implements omnibase_core's ProtocolNodeContractOverlayResolver (OMN-22471).

Registered under the onex.node_contract_overlay_resolvers entry-point group.
resolve_contract_overlay(node_id) reads the contract section of
<workspace config root>/config/onex/overlays/<node>/overlay.yaml.
Uses node_overlay_path, the file the broker binding reads (OMN-22362).
Workspace root comes from OMNIBASE_PATH, else OMNI_HOME, as node_broker.py takes it.
That says where the workspace is, never a configuration value.
No overlay answers an empty contract.
Unreadable or malformed overlays, or a contract section that is not a mapping, raise
ModelNodeContractOverlayResolutionError naming the path.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import yaml
from omnibase_core.models.contract_overlay import (
    ModelNodeContractOverlay,
    ModelNodeContractOverlayResolutionError,
)

from omnimarket.handlers.bus_binding.node_event_bus_overlay import node_overlay_path

_KEY: Final[str] = "contract"
_WORKSPACE_ROOT_NAMES: Final[tuple[str, ...]] = ("OMNIBASE_PATH", "OMNI_HOME")


class NodeContractOverlayResolver:
    """Answers a node's overlay contract section, for ``ModelNodeContractOverlay.for_node``."""

    def __init__(self, *, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def _workspace_root(self) -> Path | None:
        for name in _WORKSPACE_ROOT_NAMES:
            value = self._environ.get(name, "").strip()
            if value:
                return Path(value)
        return None

    def resolve_contract_overlay(self, node_id: str) -> ModelNodeContractOverlay:
        """The node's overlay contract section; empty with no path when there is no overlay."""
        path = node_overlay_path(node_id, self._workspace_root(), environ=self._environ)
        if path is None or not path.is_file():
            return ModelNodeContractOverlay(node_id=node_id)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ModelNodeContractOverlayResolutionError(
                f"node overlay {path} is unreadable ({type(exc).__name__})",
                overlay_path=str(path),
            ) from None
        if not isinstance(raw, dict):
            raise ModelNodeContractOverlayResolutionError(
                f"node overlay {path} must be a mapping", overlay_path=str(path)
            )
        section = raw.get(_KEY, {})
        if not isinstance(section, dict):
            raise ModelNodeContractOverlayResolutionError(
                f"node overlay {path} has a {_KEY} section that is not a mapping",
                overlay_path=str(path),
            )
        return ModelNodeContractOverlay(
            node_id=node_id, overlay_path=path, contract=section
        )


__all__ = ["NodeContractOverlayResolver"]
