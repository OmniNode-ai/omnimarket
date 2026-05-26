"""Pure compute handler for swarm endpoint selection."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import yaml

from omnimarket.nodes.node_swarm_registry_compute.models.enums import (
    EnumEndpointStatus,
    EnumSwarmCapability,
)
from omnimarket.nodes.node_swarm_registry_compute.models.model_registry_endpoint import (
    ModelRegistryEndpoint,
)
from omnimarket.nodes.node_swarm_registry_compute.models.model_swarm_endpoint_selection_request import (
    ModelEndpointHealth,
    ModelSubtask,
    ModelSwarmEndpointSelectionRequest,
)
from omnimarket.nodes.node_swarm_registry_compute.models.model_swarm_endpoint_selection_result import (
    ModelEndpointSelectionEvidence,
    ModelSwarmEndpointSelectionResult,
)

_VALID_CAPABILITIES: frozenset[str] = frozenset(c.value for c in EnumSwarmCapability)

_DEFAULT_REGISTRY_PATH = (
    Path(__file__).parent.parent / "contracts" / "endpoint_registry.yaml"
)


def _load_registry(path: Path) -> tuple[list[ModelRegistryEndpoint], str]:
    raw = path.read_text()
    data: dict[str, object] = yaml.safe_load(raw)
    registry_hash = hashlib.sha256(raw.encode()).hexdigest()

    seen_ids: set[str] = set()
    endpoints: list[ModelRegistryEndpoint] = []
    raw_endpoints = cast(list[dict[str, object]], data.get("endpoints", []))

    for ep_data in raw_endpoints:
        ep_id = str(ep_data.get("id", ""))
        if ep_id in seen_ids:
            raise ValueError(f"Duplicate endpoint id: {ep_id!r}")
        seen_ids.add(ep_id)

        base_url = str(ep_data.get("base_url", ""))
        parsed = urlparse(base_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(
                f"Unparseable base_url for endpoint {ep_id!r}: {base_url!r}"
            )

        caps = cast(list[str], ep_data.get("capabilities", []))
        invalid = [c for c in caps if c not in _VALID_CAPABILITIES]
        if invalid:
            raise ValueError(f"Unknown capabilities for endpoint {ep_id!r}: {invalid}")

        endpoints.append(ModelRegistryEndpoint.model_validate(ep_data))

    return endpoints, registry_hash


def _is_healthy(health: ModelEndpointHealth | None) -> bool:
    return health is not None and health.endpoint_status == EnumEndpointStatus.reachable


def _fits_context(endpoint: ModelRegistryEndpoint, estimated_tokens: int) -> bool:
    if endpoint.context_window is None:
        return True
    return endpoint.context_window >= estimated_tokens


def _select_for_subtask(
    subtask: ModelSubtask,
    endpoints: list[ModelRegistryEndpoint],
    endpoint_health: dict[str, ModelEndpointHealth],
) -> tuple[str | None, str]:
    """Return (endpoint_id, reason) or (None, reason) if unroutable."""
    category = subtask.category
    estimated_tokens = subtask.estimated_tokens
    affinity = subtask.model_affinity

    # Try affinity endpoint first
    if affinity:
        affinity_ep = next((ep for ep in endpoints if ep.id == affinity), None)
        if affinity_ep is not None:
            health = endpoint_health.get(affinity)
            if (
                _is_healthy(health)
                and category in affinity_ep.capabilities
                and _fits_context(affinity_ep, estimated_tokens)
            ):
                return affinity, f"affinity match: {affinity}"

    # Capability fallback — only healthy endpoints with matching capability
    capable_healthy = [
        ep
        for ep in endpoints
        if category in ep.capabilities and _is_healthy(endpoint_health.get(ep.id))
    ]

    # Prefer known-context endpoints that fit; fall back to unknown-context endpoints
    known_fit = [
        ep
        for ep in capable_healthy
        if ep.context_window is not None and _fits_context(ep, estimated_tokens)
    ]
    unknown_ctx = [ep for ep in capable_healthy if ep.context_window is None]
    ordered = known_fit if known_fit else unknown_ctx

    for ep in ordered:
        reason = f"capability match: {ep.id} supports {category!r}"
        if affinity and ep.id != affinity:
            reason += f" (affinity {affinity!r} unavailable)"
        return ep.id, reason

    return None, f"no healthy endpoint supports category {category!r}"


class HandlerSwarmRegistry:
    """Select endpoints for subtasks. Pure compute: no I/O, no side effects."""

    def __init__(self, registry_path: Path | None = None) -> None:
        path = registry_path if registry_path is not None else _DEFAULT_REGISTRY_PATH
        self._endpoints, self._registry_hash = _load_registry(path)

    def handle(
        self, request: ModelSwarmEndpointSelectionRequest
    ) -> ModelSwarmEndpointSelectionResult:
        assignments: dict[str, str] = {}
        unroutable: list[str] = []
        evidence: list[ModelEndpointSelectionEvidence] = []

        for subtask in request.subtasks:
            endpoint_id, reason = _select_for_subtask(
                subtask, self._endpoints, request.endpoint_health
            )
            if endpoint_id is not None:
                assignments[subtask.subtask_id] = endpoint_id
                evidence.append(
                    ModelEndpointSelectionEvidence(
                        subtask_id=subtask.subtask_id,
                        assigned_endpoint_id=endpoint_id,
                        reason=reason,
                    )
                )
            else:
                unroutable.append(subtask.subtask_id)

        return ModelSwarmEndpointSelectionResult(
            assignments=assignments,
            unroutable_subtasks=tuple(unroutable),
            selection_evidence=tuple(evidence),
            run_id=request.run_id,
        )


__all__ = ["HandlerSwarmRegistry"]
