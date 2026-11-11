# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Append the routing overlay's eval arm backends to a resolved backend list (OMN-23751).

The code eval pins hosted models by backend id before it has scored them. Their
declarations live in the delegation routing overlay (``eval_arms``), the one
place the deployment's ladder additions are read from, and every one is
``explicit_pin_only``. Both bifrost loaders call this module so a backend-id
pin resolves an arm through the same contract every other backend resolves
through. Nothing else selects them: the tier ladder names none of them and the
capability fallback skips pinned-only backends.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from omnimarket.handlers.routing.routing_tiers_path import (
    load_delegation_routing_overlay,
)

#: How an eval arm's backend is attributed in refusals and provenance.
EVAL_ARMS_OVERLAY_SOURCE = "DELEGATION_ROUTING_OVERLAY_PATH eval_arms"


def append_eval_arm_backends(
    backends: Sequence[Mapping[str, Any]],
    *,
    provider_rules: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Return ``backends`` with the routing overlay's eval arm backends appended.

    An eval arm never overrides a contract or overlay backend: a colliding
    ``backend_id`` raises naming the id. Each appended row is proven complete by
    ``validate_overlay_added_backends`` and its endpoint is held to the
    contract's declared provider surfaces. An unbound overlay appends nothing.
    """
    # Imported here: the canonical bifrost loader imports this module, so a
    # module-level import back into it would be a cycle.
    from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
        reject_backends_off_a_declared_provider_surface,
        validate_overlay_added_backends,
    )

    merged = [dict(b) for b in backends]
    rows = load_delegation_routing_overlay().eval_backend_rows()
    if not rows:
        return merged
    existing = {str(b.get("backend_id")) for b in merged}
    clash = sorted(str(r["backend_id"]) for r in rows if r["backend_id"] in existing)
    if clash:
        msg = (
            f"{EVAL_ARMS_OVERLAY_SOURCE}: eval arm backend_id(s) {clash} collide with "
            "a routing-contract backend; an eval arm never overrides one"
        )
        raise ValueError(msg)
    merged.extend(
        validate_overlay_added_backends(
            merged, rows, overlay_source=EVAL_ARMS_OVERLAY_SOURCE
        )
    )
    reject_backends_off_a_declared_provider_surface(
        merged, provider_rules, source=EVAL_ARMS_OVERLAY_SOURCE
    )
    return merged


def with_eval_arm_backends(data: Mapping[str, Any]) -> dict[str, Any]:
    """The merged routing-contract mapping with the eval arm backends appended.

    The contract's provider surfaces are enforced on the result by the loader's
    own validation of the whole mapping, so they are not passed here.
    """
    return {**data, "backends": append_eval_arm_backends(data.get("backends") or [])}


__all__: list[str] = [
    "EVAL_ARMS_OVERLAY_SOURCE",
    "append_eval_arm_backends",
    "with_eval_arm_backends",
]
