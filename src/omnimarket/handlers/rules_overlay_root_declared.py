# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay root an installed unit must read from, declared by the lab host table (OMN-23711).

The schedule installers pin the overlay root into the unit from whatever ``ONEX_SKILL_OVERLAY_ROOTS``
the installing shell had, so three units on a lab host read a stale scratch clone. The install must
refuse a root that is not the overlays directory of the clone the lab host table declares for the
host. Everything here is pure except the one read of the table beside the pinned root.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

#: The overlay in the table that declares each host's clones.
HOST_TABLE_OVERLAY = "lab_hosts"

#: The repository the declared overlays clone lives in.
OVERLAYS_REPO = "OmniNode-ai/overlays"

#: The ref the declared overlays clone is checked out at.
OVERLAYS_REF = "main"

#: The variable the table's clone paths use to name the host's home.
OMNI_HOME_VARIABLE = "$OMNI_HOME"


class OverlayRootRefusedError(ValueError):
    """The pinned overlay root is not the one the lab host table declares for the host."""


def _clone_path(clone: Any, omni_root: Path) -> Path:
    """The clone's path with ``$OMNI_HOME`` expanded to ``omni_root``."""
    path = str(clone["path"])
    if OMNI_HOME_VARIABLE in path:
        path = path.replace(OMNI_HOME_VARIABLE, str(omni_root))
    return Path(path)


def declared_overlay_root(
    hosts_document: Mapping[str, Any], host: str, omni_root: Path
) -> Path:
    """The overlays directory the lab host table declares for ``host``.

    From the parsed table's ``hosts`` list, find the row named ``host`` (refuse when none), the
    clones whose repo is :data:`OVERLAYS_REPO` (refuse when there are none, or more than one, or the
    one is not at :data:`OVERLAYS_REF`), and return the clone's path -- ``$OMNI_HOME`` expanded to
    ``omni_root`` -- joined with ``overlays``. Every refusal names the host and the reason.
    """
    rows = hosts_document.get("hosts")
    if not isinstance(rows, list):
        raise OverlayRootRefusedError(
            f"{host}: the lab host table has no 'hosts' list to declare its overlays clone"
        )
    row = next(
        (item for item in rows if isinstance(item, dict) and item.get("name") == host),
        None,
    )
    if row is None:
        raise OverlayRootRefusedError(
            f"{host}: the lab host table declares no row for this host, so its overlays clone is unknown"
        )
    clones = row.get("clones")
    if not isinstance(clones, list) or not clones:
        raise OverlayRootRefusedError(
            f"{host}: the lab host table row declares no clones, so its overlays clone is unknown"
        )
    overlays = [
        clone
        for clone in clones
        if isinstance(clone, dict) and clone.get("repo") == OVERLAYS_REPO
    ]
    if not overlays:
        raise OverlayRootRefusedError(
            f"{host}: the lab host table declares no clone of {OVERLAYS_REPO}, so its overlays clone is unknown"
        )
    if len(overlays) > 1:
        raise OverlayRootRefusedError(
            f"{host}: the lab host table declares more than one clone of {OVERLAYS_REPO}, so its overlays clone is ambiguous"
        )
    clone = overlays[0]
    if clone.get("ref") != OVERLAYS_REF:
        raise OverlayRootRefusedError(
            f"{host}: the lab host table declares the {OVERLAYS_REPO} clone at ref {clone.get('ref')!r}, not {OVERLAYS_REF!r}"
        )
    return _clone_path(clone, omni_root) / "overlays"


def refuse_undeclared_overlay_root(
    overlay_root: Path, *, host: str, omni_root: Path
) -> None:
    """Raise :class:`OverlayRootRefusedError` when ``overlay_root`` is not the declared root.

    Reads ``<overlay_root>/lab_hosts/overlay.yaml`` (refuse when it is absent), computes the root the
    table declares for ``host``, and refuses when the two differ.
    """
    table = overlay_root / HOST_TABLE_OVERLAY / "overlay.yaml"
    if not table.is_file():
        raise OverlayRootRefusedError(
            f"{host}: {table} is not present, so the pinned overlay root {overlay_root} is not a declared overlays directory"
        )
    document = yaml.safe_load(table.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise OverlayRootRefusedError(
            f"{host}: {table} is not a mapping, so the pinned overlay root {overlay_root} is not a declared overlays directory"
        )
    declared = declared_overlay_root(document, host, omni_root)
    if overlay_root.resolve() != declared.resolve():
        raise OverlayRootRefusedError(
            f"{host}: the pinned overlay root {overlay_root} is not the declared root {declared}"
        )
