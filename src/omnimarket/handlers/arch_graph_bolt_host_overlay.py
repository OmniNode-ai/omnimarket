# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the architecture graph Bolt URI: the variable, then the host list of the overlay (OMN-24687).

``node_architecture_graph_query_effect`` and ``node_architecture_graph_populate_effect``
bind ``bolt_uri`` to the contract reference ``${env.ARCH_GRAPH_BOLT_URI}``. The dev lane's
containers get the variable from the runtime policy contract; a lab host or the operator Mac
that sets nothing had no address, so both nodes refused. This module adds the second source:
the hosts list of ``<overlay root>/arch_graph_bolt/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS``, validated through :class:`ModelArchGraphBoltOverlay`, the same
shape as the projection read binding host overlay (OMN-23994).

The variable keeps winning. A host not in the list, or no overlay file, fails closed; a found
overlay that does not load raises and never falls back, so a broken overlay reads as broken,
not as absent.
"""

from __future__ import annotations

from omnibase_infra.runtime.overlay.contract_env_ref import expand_contract_env_refs

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
    local_host_short_name,
)
from omnimarket.models.node_overlays.model_arch_graph_bolt_overlay import (
    ModelArchGraphBoltOverlay,
)

ARCH_GRAPH_BOLT_OVERLAY_ID = "arch_graph_bolt"
_BOLT_URI_CONTRACT_REF = "${env.ARCH_GRAPH_BOLT_URI}"


def _host_row_bolt_uri(host: str) -> str | None:
    if find_node_overlay(ARCH_GRAPH_BOLT_OVERLAY_ID) is None:
        return None
    try:
        overlay = load_structured_overlay(
            ARCH_GRAPH_BOLT_OVERLAY_ID, ModelArchGraphBoltOverlay
        )
    except NodeOverlayError as exc:
        raise ValueError(
            f"overlay {ARCH_GRAPH_BOLT_OVERLAY_ID} does not load "
            f"({type(exc).__name__}: {exc}); fix the document, no lower source is tried"
        ) from exc
    wanted = host.casefold()
    if any(name.casefold() == wanted for name in overlay.hosts):
        return overlay.bolt_uri
    return None


def resolve_arch_graph_bolt_uri(host: str | None = None) -> str:
    """The Bolt URI: ``ARCH_GRAPH_BOLT_URI``, else this host's overlay row, else a refusal."""
    resolved = expand_contract_env_refs(_BOLT_URI_CONTRACT_REF)
    if resolved:
        return resolved
    row_uri = _host_row_bolt_uri(host if host is not None else local_host_short_name())
    if row_uri:
        return row_uri
    raise ValueError(
        "ARCH_GRAPH_BOLT_URI is not bound by the active overlay; set the variable or "
        f"add this host to the hosts list of <overlay root>/{ARCH_GRAPH_BOLT_OVERLAY_ID}/overlay.yaml "
        f"so the contract reference {_BOLT_URI_CONTRACT_REF!r} resolves to a Bolt URI."
    )


__all__ = [
    "ARCH_GRAPH_BOLT_OVERLAY_ID",
    "resolve_arch_graph_bolt_uri",
]
