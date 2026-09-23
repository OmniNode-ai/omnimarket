# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tier-ladder placement of a lane-added delegation backend (OMN-19215).

A lane overlay may ADD a delegation backend (OMN-17099), but routing only
offers a backend that ``routing_tiers.yaml`` names in a tier's ``models``, and
that file ships in this package. So an added backend such as the second lab
host serving the same model as the first was reachable only by a per-run
``backend_id`` pin.

A ``placement`` on the bifrost backend entry names a tier and the existing
rungs the added backend is a fallback for. :func:`apply_backend_placements`
appends one mirrored tier entry per rung AFTER the tier's existing models, so
routing's declaration-order selection reaches the placed backend only when the
rung it mirrors is unroutable or already tried (the transport-failure sibling
probe), and never ahead of it. Load-spreading across same-model rungs is a
separate design and is not done here.

The routing authority applies placements when it loads the ladder, so the
reducer, the same-tier sibling probe and the local dispatch path all read one
placed ladder, and :func:`placement_digest` lets the replay-provenance hash
cover the placement as well as the tiers file.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.inference.delegation_config_provenance import (
    resolve_bifrost_path_binding,
)
from omnimarket.models.delegation.wire import (
    ModelDelegationConfig,
    ModelRoutingTier,
    ModelTierModel,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
    ModelDelegationBackendPlacement,
)


def _refuse(backend_id: str, reason: str) -> ProtocolConfigurationError:
    return ProtocolConfigurationError(
        f"Bifrost backend {backend_id!r} declares an invalid tier placement: "
        f"{reason} (OMN-19215)."
    )


def _mirror(
    rung: ModelTierModel,
    backend: ModelDelegationBackendConfig,
    placement: ModelDelegationBackendPlacement,
) -> ModelTierModel:
    max_context = min(rung.max_context_tokens, placement.max_context_tokens)
    fast_path = rung.fast_path_threshold_tokens
    return ModelTierModel(
        # The placed backend's own served id: routing sends a local tier's
        # entry id as the request model, and the served-model guard checks it.
        id=backend.model_name or "",
        backend_ref=backend.backend_id,
        max_context_tokens=max_context,
        use_for=rung.use_for,
        fast_path_threshold_tokens=(
            None if fast_path is None else min(fast_path, max_context)
        ),
    )


def apply_backend_placements(
    config: ModelDelegationConfig,
    backends: Sequence[ModelDelegationBackendConfig],
) -> ModelDelegationConfig:
    """Return ``config`` with every placed backend mirrored into its tier.

    Returns ``config`` itself when no backend declares a placement. Raises
    :class:`ProtocolConfigurationError` naming the backend when a placement
    names an unknown tier, a rung that tier does not declare, a backend with no
    served model name, or a backend the tier already carries.
    """
    placed = [
        (backend, placement)
        for backend in backends
        if (placement := backend.placement) is not None
    ]
    if not placed:
        return config

    tiers: dict[str, ModelRoutingTier] = {tier.name: tier for tier in config.tiers}
    for backend, placement in placed:
        tier = tiers.get(placement.tier)
        if tier is None:
            raise _refuse(
                backend.backend_id,
                f"tier {placement.tier!r} is not in the routing ladder "
                f"(tiers: {sorted(tiers)})",
            )
        if not (backend.model_name or "").strip():
            raise _refuse(
                backend.backend_id,
                "the backend declares no model_name, so a mirrored entry would "
                "name no served model",
            )
        declared = {model.backend_ref: model for model in tier.models}
        if backend.backend_id in declared:
            raise _refuse(
                backend.backend_id,
                f"tier {tier.name!r} already carries this backend",
            )
        mirrors: list[ModelTierModel] = []
        for rung_ref in placement.fallback_for:
            rung = declared.get(rung_ref)
            if rung is None:
                raise _refuse(
                    backend.backend_id,
                    f"fallback rung {rung_ref!r} is not declared in tier "
                    f"{tier.name!r} (rungs: {sorted(declared)})",
                )
            mirrors.append(_mirror(rung, backend, placement))
        tiers[tier.name] = tier.model_copy(update={"models": (*tier.models, *mirrors)})

    return config.model_copy(
        update={"tiers": tuple(tiers[tier.name] for tier in config.tiers)}
    )


def placement_digest(backends: Sequence[ModelDelegationBackendConfig]) -> str | None:
    """SHA-256 of every declared placement, or None when there is none."""
    entries = sorted(
        json.dumps(
            [backend.backend_id, backend.model_name, placement.model_dump(mode="json")],
            sort_keys=True,
            separators=(",", ":"),
        )
        for backend in backends
        if (placement := backend.placement) is not None
    )
    if not entries:
        return None
    return hashlib.sha256("\n".join(entries).encode()).hexdigest()


def load_bound_bifrost_backends() -> tuple[ModelDelegationBackendConfig, ...]:
    """Load the bifrost backends through the one contract+overlay binding seam.

    The same binding the routing authority's endpoint loader uses (OMN-18676),
    so placements are read from exactly the contract routing resolves
    endpoints from.
    """
    binding = resolve_bifrost_path_binding()
    config = load_bifrost_delegation_config(
        config_path=binding.contract_path,
        overlay_path=binding.overlay_path,
    )
    return config.backends


__all__: list[str] = [
    "apply_backend_placements",
    "load_bound_bifrost_backends",
    "placement_digest",
]
