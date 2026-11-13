# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the ordered overlay root list (OMN-24032).

Effect: the filesystem is reached only through the injected probe.
Order: (1) the store root, (2) the canonical overlays clone(s) beside the
registry root, (3) the declared roots in their given order. The retired path
<registry root>/overlays/overlays is never produced as a store or canonical
clone. An absent root is named in the finding, never an exception.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.overlay_roots import (
    EnumOverlayRootSource,
    ModelOverlayRootProbe,
    ModelOverlayRootsRequest,
    ModelOverlayRootsResult,
)
from omnimarket.nodes.node_overlay_roots_resolve_effect.adapters.filesystem_probe import (
    FileSystemOverlayRootsProbe,
)
from omnimarket.nodes.node_overlay_roots_resolve_effect.protocols.protocol_overlay_roots_probe import (
    ProtocolOverlayRootsProbe,
)

_NODE = "node_overlay_roots_resolve_effect"


def contract_config() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{_NODE}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded = yaml.safe_load(text)
    config = loaded.get("config") if isinstance(loaded, dict) else None
    if not isinstance(config, dict):
        raise ValueError(f"{_NODE}: contract config must be a mapping")
    return config


def _required(config: dict[str, Any], key: str) -> Any:
    if key not in config:
        raise ValueError(f"{_NODE}: contract config is missing {key!r}")
    return config[key]


def _expand(value: str, home: str | None) -> str | None:
    if value == "~":
        return home
    if value.startswith("~/"):
        return f"{home}/{value[2:]}" if home is not None else None
    return value


def _snapshot_value(text: str, key: str) -> str | None:
    prefix = f"{key}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            value = value.split(" #", 1)[0].strip()
            return value
    return None


class HandlerOverlayRootsResolve:
    def __init__(self, probe: ProtocolOverlayRootsProbe | None = None) -> None:
        self._probe = probe if probe is not None else FileSystemOverlayRootsProbe()

    def handle(self, request: ModelOverlayRootsRequest) -> ModelOverlayRootsResult:
        config = contract_config()
        overlay_file = _required(config, "overlay_file_name")
        store_id = _required(config, "store_read_overlay_id")
        snapshot_key = _required(config, "store_snapshot_key")
        home_relpath = _required(config, "store_home_relpath")
        clone_relpaths = _required(config, "canonical_clone_relpaths")

        probe = self._probe
        clone_candidates: list[str] = []
        if request.registry_root:
            parent = Path(probe.resolve(request.registry_root)).parent
            for relpath in clone_relpaths:
                clone_candidates.append(str(parent / relpath))

        bootstrap = [c for c in clone_candidates if probe.is_dir(c)]
        bootstrap.extend(request.declared_roots)

        store_candidate: str | None = None
        store_named_by: str | None = None
        for root in bootstrap:
            overlay_path = f"{root}/{store_id}/{overlay_file}"
            text = probe.read_text(overlay_path)
            if text is None:
                continue
            value = _snapshot_value(text, snapshot_key)
            if value is None:
                continue
            expanded = _expand(value, request.home)
            if expanded is None:
                continue
            store_candidate = expanded
            store_named_by = root
            break
        if store_candidate is None and request.home is not None:
            store_candidate = str(Path(request.home) / home_relpath)

        candidates: list[tuple[str, EnumOverlayRootSource]] = []
        if store_candidate is not None:
            candidates.append((store_candidate, EnumOverlayRootSource.STORE))
        for path in clone_candidates:
            candidates.append((path, EnumOverlayRootSource.CANONICAL_CLONE))
        for path in request.declared_roots:
            expanded = _expand(path, request.home)
            if expanded is not None:
                candidates.append((expanded, EnumOverlayRootSource.DECLARED))

        seen: set[str] = set()
        considered: list[ModelOverlayRootProbe] = []
        roots: list[ModelOverlayRootProbe] = []
        absent: list[str] = []
        for path, source in candidates:
            if path in seen:
                continue
            seen.add(path)
            present = probe.is_dir(path)
            considered.append(
                ModelOverlayRootProbe(path=path, source=source, present=present)
            )
            if present:
                roots.append(
                    ModelOverlayRootProbe(path=path, source=source, present=True)
                )
            else:
                absent.append(
                    f"overlay root {path} ({source.value}) is not a directory"
                )

        finding = "; ".join(absent) if absent else None
        return ModelOverlayRootsResult(
            roots=tuple(roots),
            considered=tuple(considered),
            store_named_by=store_named_by,
            finding=finding,
        )
