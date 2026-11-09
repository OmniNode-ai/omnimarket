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
probe), and never ahead of it.

A placement whose ``mode`` is ``spread`` (AC4, RULING ledger:4257) is mirrored
the same way, and :func:`spread_groups` also names it as a first-choice peer of
each rung it mirrors. The routing reducer then picks one member of the group per
request by a stable hash of the correlation id, so a second host serving the
same model shares the load instead of idling behind the first. The ladder order
is unchanged, so the failover behaviour above still holds for every member.

The routing authority applies placements when it loads the ladder, so the
reducer, the same-tier sibling probe and the local dispatch path all read one
placed ladder, and :func:`placement_digest` lets the replay-provenance hash
cover the placement as well as the tiers file.

A placement whose ``mode`` is ``lead`` (OMN-23329) is mirrored BEFORE the tier's
own rungs, as a :class:`ModelLeadTierModel`, so the routing reducer tries it first
for the classes it names and falls through to the tier's rungs for any request it
cannot take.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence

from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_backend_placements,
)
from omnimarket.enums.enum_backend_placement_mode import EnumBackendPlacementMode
from omnimarket.inference.delegation_config_provenance import (
    resolve_bifrost_path_binding,
)
from omnimarket.models.delegation.model_delegation_backend_placement import (
    ModelDelegationBackendPlacement,
    ModelPlacedDelegationBackend,
)
from omnimarket.models.delegation.wire import (
    ModelDelegationConfig,
    ModelRoutingTier,
    ModelTierModel,
)


class ModelLeadTierModel(ModelTierModel):
    """A tier entry mirrored by a ``lead`` placement (OMN-23329).

    The same entry as its parent, kept as its own type so the routing reducer can
    tell a lead rung from the tier's own rungs without a second source of truth
    that could drift from the ladder it was applied to.
    """


def _entry_type(placement: ModelDelegationBackendPlacement) -> type[ModelTierModel]:
    if placement.mode is EnumBackendPlacementMode.LEAD:
        return ModelLeadTierModel
    return ModelTierModel


def lead_models(tier_models: Sequence[ModelTierModel]) -> tuple[ModelTierModel, ...]:
    """The lead-placed entries of a tier, in ladder order (OMN-23329)."""
    return tuple(m for m in tier_models if isinstance(m, ModelLeadTierModel))


def lead_refs_in(config: ModelDelegationConfig) -> frozenset[str]:
    """The ``backend_ref`` of every lead-placed entry in ``config`` (OMN-23329)."""
    return frozenset(
        m.backend_ref for tier in config.tiers for m in lead_models(tier.models)
    )


def _refuse(backend_id: str, reason: str) -> ProtocolConfigurationError:
    return ProtocolConfigurationError(
        f"Bifrost backend {backend_id!r} declares an invalid tier placement: "
        f"{reason} (OMN-19215)."
    )


def _mirror(
    rung: ModelTierModel,
    backend: ModelPlacedDelegationBackend,
    placement: ModelDelegationBackendPlacement,
) -> ModelTierModel:
    max_context = min(rung.max_context_tokens, placement.max_context_tokens)
    fast_path = rung.fast_path_threshold_tokens
    use_for = rung.use_for
    if placement.use_for is not None:
        use_for = tuple(task for task in rung.use_for if task in placement.use_for)
        if not use_for:
            raise _refuse(
                backend.backend_id,
                f"use_for {sorted(placement.use_for)} shares no task class with "
                f"rung {rung.backend_ref!r} (rung use_for: {sorted(rung.use_for)})",
            )
    return _entry_type(placement)(
        # The placed backend's own served id: routing sends a local tier's
        # entry id as the request model, and the served-model guard checks it.
        id=backend.model_name,
        backend_ref=backend.backend_id,
        max_context_tokens=max_context,
        use_for=use_for,
        fast_path_threshold_tokens=(
            None if fast_path is None else min(fast_path, max_context)
        ),
    )


def _standalone(
    backend: ModelPlacedDelegationBackend,
    placement: ModelDelegationBackendPlacement,
) -> ModelTierModel:
    """A rung declared by the placement alone (OMN-20376), with its own use_for."""
    assert placement.use_for is not None  # the placement model enforces it
    fast_path = placement.fast_path_threshold_tokens
    return _entry_type(placement)(
        id=backend.model_name,
        backend_ref=backend.backend_id,
        max_context_tokens=placement.max_context_tokens,
        use_for=placement.use_for,
        fast_path_threshold_tokens=(
            None if fast_path is None else min(fast_path, placement.max_context_tokens)
        ),
    )


def apply_backend_placements(
    config: ModelDelegationConfig,
    placed: Sequence[ModelPlacedDelegationBackend],
) -> ModelDelegationConfig:
    """Return ``config`` with every placed backend mirrored into its tier.

    Returns ``config`` itself when ``placed`` is empty. Raises
    :class:`ProtocolConfigurationError` naming the backend when a placement
    names an unknown tier, a rung that tier does not declare, a backend with no
    served model name, or a backend the tier already carries.
    """
    if not placed:
        return config

    tiers: dict[str, ModelRoutingTier] = {tier.name: tier for tier in config.tiers}
    for backend in placed:
        placement = backend.placement
        tier = tiers.get(placement.tier)
        if tier is None:
            raise _refuse(
                backend.backend_id,
                f"tier {placement.tier!r} is not in the routing ladder "
                f"(tiers: {sorted(tiers)})",
            )
        if not backend.model_name.strip():
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
        if not placement.fallback_for:
            mirrors.append(_standalone(backend, placement))
        for rung_ref in placement.fallback_for:
            rung = declared.get(rung_ref)
            if rung is None:
                raise _refuse(
                    backend.backend_id,
                    f"fallback rung {rung_ref!r} is not declared in tier "
                    f"{tier.name!r} (rungs: {sorted(declared)})",
                )
            mirrors.append(_mirror(rung, backend, placement))
        if placement.mode is EnumBackendPlacementMode.LEAD:
            models = (*mirrors, *tier.models)
        else:
            models = (*tier.models, *mirrors)
        tiers[tier.name] = tier.model_copy(update={"models": models})

    return config.model_copy(
        update={"tiers": tuple(tiers[tier.name] for tier in config.tiers)}
    )


def spread_groups(
    placed: Sequence[ModelPlacedDelegationBackend],
) -> dict[str, tuple[str, ...]]:
    """Map each rung to the spread-mode backends that share its traffic.

    Keyed by the rung's ``backend_ref``; each value lists the placed backends,
    in declaration order, whose placement is ``spread`` and names that rung in
    ``fallback_for``. A rung with no spread peer is absent. Fallback-mode
    placements never appear, so a ladder with no spread placement yields ``{}``
    and routing is byte-identical to the fallback-only behaviour.
    """
    groups: dict[str, list[str]] = {}
    for backend in placed:
        if backend.placement.mode is not EnumBackendPlacementMode.SPREAD:
            continue
        for rung_ref in backend.placement.fallback_for:
            peers = groups.setdefault(rung_ref, [])
            if backend.backend_id not in peers:
                peers.append(backend.backend_id)
    return {rung: tuple(peers) for rung, peers in groups.items()}


def pick_spread_member(
    selected: ModelTierModel,
    tier_models: Sequence[ModelTierModel],
    task_type: str,
    estimated_tokens: int,
    exclude_backend_refs: frozenset[str],
    spread_peers: dict[str, tuple[str, ...]],
    member_weights: dict[str, float],
    spread_key: str,
    routable: Callable[[str], bool],
) -> ModelTierModel:
    """One member of ``selected``'s spread group, by stable hash of ``spread_key``.

    The group is ``selected`` plus each spread peer of it that is not excluded, is
    ``routable``, declares ``task_type`` and fits ``estimated_tokens``; each member
    takes the share its placement weight declares (``selected``'s is 1.0). A group
    of one returns ``selected`` (OMN-19215 AC4).
    """
    group: list[ModelTierModel] = [selected]
    for peer_ref in spread_peers.get(selected.backend_ref, ()):
        if peer_ref in exclude_backend_refs or not routable(peer_ref):
            continue
        member = next(
            (
                model
                for model in tier_models
                if model.backend_ref == peer_ref
                and task_type in model.use_for
                and estimated_tokens <= model.max_context_tokens
            ),
            None,
        )
        if member is not None:
            group.append(member)
    if len(group) == 1:
        return selected
    weights = [1.0, *(member_weights.get(m.backend_ref, 1.0) for m in group[1:])]
    return group[spread_pick(spread_key, weights)]


def spread_weights(
    placed: Sequence[ModelPlacedDelegationBackend],
) -> dict[str, float]:
    """Map each spread-mode placed backend to its declared ``weight``.

    Only spread placements appear. A rung is not a placed backend and always
    carries weight 1.0, so a peer's weight reads relative to the rung it shares
    traffic with (:func:`spread_pick`).
    """
    return {
        backend.backend_id: backend.placement.weight
        for backend in placed
        if backend.placement.mode is EnumBackendPlacementMode.SPREAD
    }


def spread_index(spread_key: str, members: int) -> int:
    """Stable member index for ``spread_key`` in a group of ``members``.

    SHA-256 rather than ``hash()``: Python salts ``str`` hashes per process, and
    the same correlation id must pick the same member in every process that
    routes it (the orchestrator, a replay, a retry after a restart).
    """
    if members < 1:
        raise ValueError(f"a spread group has at least one member, got {members}")
    digest = hashlib.sha256(spread_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % members


def spread_pick(spread_key: str, weights: Sequence[float]) -> int:
    """Stable member index for ``spread_key``, each member taking its weight's share.

    Equal weights reduce to a uniform pick like :func:`spread_index`. The same
    SHA-256 digest of the key is mapped onto the cumulative weights, so the same
    correlation id picks the same member in every process for a given group.
    """
    if not weights:
        raise ValueError("a spread group has at least one member, got 0")
    if any(weight <= 0 for weight in weights):
        raise ValueError(f"spread weights must be positive, got {list(weights)}")
    digest = hashlib.sha256(spread_key.encode("utf-8")).digest()
    point = int.from_bytes(digest[:8], "big") / 2**64 * sum(weights)
    running = 0.0
    for index, weight in enumerate(weights):
        running += weight
        if point < running:
            return index
    return len(weights) - 1


def placement_digest(placed: Sequence[ModelPlacedDelegationBackend]) -> str | None:
    """SHA-256 of every declared placement, or None when there is none."""
    entries = sorted(
        json.dumps(
            backend.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        for backend in placed
    )
    if not entries:
        return None
    return hashlib.sha256("\n".join(entries).encode()).hexdigest()


def load_bound_bifrost_placements() -> tuple[ModelPlacedDelegationBackend, ...]:
    """The placed backends, read through the one contract+overlay binding seam.

    The same binding the routing authority's endpoint loader uses (OMN-18676),
    so placements come from exactly the contract routing resolves endpoints
    from.
    """
    binding = resolve_bifrost_path_binding()
    return load_bifrost_backend_placements(
        config_path=binding.contract_path,
        overlay_path=binding.overlay_path,
    )


__all__: list[str] = [
    "ModelLeadTierModel",
    "apply_backend_placements",
    "lead_models",
    "lead_refs_in",
    "load_bound_bifrost_placements",
    "pick_spread_member",
    "placement_digest",
    "spread_groups",
    "spread_index",
    "spread_pick",
    "spread_weights",
]
