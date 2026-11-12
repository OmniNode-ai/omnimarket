# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Bind the routing overlay's local endpoints onto a resolved backend list (OMN-23871).

A deployment's local rung is a GPU backend served on the machine itself. Its
complete endpoint and the model it serves are deployment facts, so they are
declared in the delegation routing overlay (``local_endpoints``) and bound by
both bifrost loaders onto the routing contract's local backends: an override
row sets the endpoint and served model of the contract backend of that id, and
a machine with no machine-local ``bifrost_overrides.yaml`` still resolves the
local rung.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from omnimarket.handlers.routing.routing_tiers_path import (
    load_delegation_routing_overlay,
)

#: How a local endpoint row is attributed in refusals and provenance (OMN-23871).
LOCAL_ENDPOINTS_OVERLAY_SOURCE = "DELEGATION_ROUTING_OVERLAY_PATH local_endpoints"


def bind_local_endpoint_backends(
    backends: Sequence[Mapping[str, Any]],
    *,
    provider_rules: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Return ``backends`` with the routing overlay's local endpoints bound.

    An override row sets the ``endpoint_url`` and ``model_name`` (and any other
    key it states) on the contract backend of that id; an unknown id adds a
    complete backend, proven complete by ``validate_overlay_added_backends``.
    The overlay's declaration wins over a machine-local
    ``bifrost_overrides.yaml`` because it is applied after it; an absent
    section appends nothing and changes nothing.
    """
    # Imported here: the canonical bifrost loader imports this module, so a
    # module-level import back into it would be a cycle.
    from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
        deep_merge_bifrost_delegation_config,
        reject_backends_off_a_declared_provider_surface,
        validate_overlay_added_backends,
    )

    merged = [dict(b) for b in backends]
    rows = load_delegation_routing_overlay().local_endpoint_backend_rows()
    if not rows:
        return merged
    contract_ids = {str(b.get("backend_id")) for b in merged}
    for row in rows:
        if row["backend_id"] not in contract_ids:
            # An added contract row states these keys even when null (OMN-17099).
            row.setdefault("secret_ref", None)
            row.setdefault("endpoint_url", None)
    validate_overlay_added_backends(
        merged, rows, overlay_source=LOCAL_ENDPOINTS_OVERLAY_SOURCE
    )
    result = deep_merge_bifrost_delegation_config(
        {"backends": merged}, {"backends": rows}
    )["backends"]
    reject_backends_off_a_declared_provider_surface(
        result, provider_rules, source=LOCAL_ENDPOINTS_OVERLAY_SOURCE
    )
    return list(result)


__all__: list[str] = [
    "LOCAL_ENDPOINTS_OVERLAY_SOURCE",
    "bind_local_endpoint_backends",
]
