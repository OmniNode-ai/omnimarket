# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep the lab SLI pack off customer screens unless a deployment opts in.

The pack is off by default; a deployment must explicitly enable it in its own
contract overlay before any of its nodes can run.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import NodeOverlayError, load_node_overlay

LAB_SLI_PACK_OVERLAY = "lab_sli_pack"
LAB_SLI_PACK_FIELDS = frozenset({"status"})


class LabSliPackDisabledError(NodeOverlayError):
    """The deployment has not explicitly enabled the lab SLI pack."""


def lab_sli_pack_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether the deployment enables the pack; reject broken overlays."""
    status = load_node_overlay(
        LAB_SLI_PACK_OVERLAY, fields=LAB_SLI_PACK_FIELDS, environ=environ
    ).get("status")
    if status is None or status == "disabled":
        return False
    if status == "enabled":
        return True
    raise NodeOverlayError(
        f"{LAB_SLI_PACK_OVERLAY}: status must be 'enabled' or 'disabled'; got {status!r}"
    )


def require_lab_sli_pack(
    node_name: str, environ: Mapping[str, str] | None = None
) -> None:
    """Refuse a node invocation until its deployment explicitly enables the pack."""
    if not lab_sli_pack_enabled(environ):
        raise LabSliPackDisabledError(
            f"{node_name}: the lab SLI pack is off by default; enable it with an "
            f"overlay with `status: enabled` at <root>/{LAB_SLI_PACK_OVERLAY}/overlay.yaml "
            "under a root named in ONEX_SKILL_OVERLAY_ROOTS"
        )
