# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the bus a node is reached on from the node's contract overlay (OMN-22351).

Operator ruling 2026-11-03: "The default broker should be in the contract overlay." A client of a
node that is given no ``--bus-lane`` and no ``--kafka-bootstrap`` reads ``event_bus`` from the
node's overlay, found in the one overlay root order: the store root first, then the canonical
overlays clone beside the registry root, then the declared roots, and last the frozen config root
``<workspace config root>/config/onex/overlays/<node>/overlay.yaml``. The workspace config root is
the one the lane declaration is read under, resolved from the same ``--omnibase-path`` workspace
root, so no flag, environment variable or packaged default selects the broker.

An overlay with no ``event_bus`` key, or no overlay, answers None and the caller refuses as before,
naming the file it looked for. A present ``event_bus`` that is malformed raises: a broken overlay
must not read as an absent one. The overlay's other keys belong to other readers and are left alone.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import yaml
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus
from omnimarket.models.overlay_roots import ModelOverlayRootsRequest
from omnimarket.nodes.node_overlay_roots_resolve_effect.handlers.handler_overlay_roots_resolve import (
    HandlerOverlayRootsResolve,
)

NODE_OVERLAYS_RELATIVE_PATH: Final[Path] = Path("config") / "onex" / "overlays"
_OVERLAY_FILE: Final[str] = "overlay.yaml"
_KEY: Final[str] = "event_bus"


def node_overlay_path(
    node_name: str,
    workspace_root: Path | None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """The first overlay file the overlay root resolver names, else the frozen config root path."""
    env = os.environ if environ is None else environ
    declared = [
        root for root in env.get(OVERLAY_ROOTS_ENV, "").split(os.pathsep) if root
    ]
    registry_root = None
    if workspace_root is not None:
        registry_root = str(workspace_root)
    else:
        for name in ("OMNIBASE_PATH", "OMNI_HOME"):
            value = env.get(name, "").strip()
            if value:
                registry_root = value
                break
    request = ModelOverlayRootsRequest(
        declared_roots=tuple(declared),
        registry_root=registry_root,
        home=env.get("HOME") or None,
    )
    result = HandlerOverlayRootsResolve().handle(request)
    for root in result.paths():
        candidate = Path(root) / node_name / _OVERLAY_FILE
        if candidate.is_file():
            return candidate
    if workspace_root is None:
        return None
    from omnibase_infra.handlers.handler_workspace_config_root_resolve import (
        HandlerWorkspaceConfigRootResolve,
    )
    from omnibase_infra.models.workspace import ModelWorkspaceConfigRootRequest

    resolution = HandlerWorkspaceConfigRootResolve().handle(
        ModelWorkspaceConfigRootRequest(workspace_root=workspace_root)
    )
    if resolution.config_root is None:
        return None
    return (
        resolution.config_root / NODE_OVERLAYS_RELATIVE_PATH / node_name / _OVERLAY_FILE
    )


def read_node_event_bus(path: Path | None) -> ModelNodeEventBus | None:
    """The overlay's ``event_bus``, or None when the file or the key is absent."""
    if path is None or not path.is_file():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise LabRunBusError(
            f"node overlay {path} is unreadable ({type(exc).__name__})"
        ) from None
    if not isinstance(raw, dict):
        raise LabRunBusError(f"node overlay {path} must be a mapping")
    if _KEY not in raw:
        return None
    try:
        return ModelNodeEventBus.model_validate(raw[_KEY])
    except ValidationError as exc:
        fields = sorted(
            ".".join(str(part) for part in err["loc"]) or _KEY
            for err in exc.errors(include_input=False, include_context=False)
        )
        raise LabRunBusError(
            f"node overlay {path} has an invalid {_KEY}: fields {fields}"
        ) from None


def resolve_node_event_bus(
    node_name: str,
    workspace_root: Path | None,
    environ: Mapping[str, str] | None = None,
) -> tuple[ModelNodeEventBus | None, Path | None]:
    """The node's declared bus and the overlay path looked at (None when no config root resolves)."""
    path = node_overlay_path(node_name, workspace_root, environ)
    return read_node_event_bus(path), path


__all__ = [
    "NODE_OVERLAYS_RELATIVE_PATH",
    "node_overlay_path",
    "read_node_event_bus",
    "resolve_node_event_bus",
]
