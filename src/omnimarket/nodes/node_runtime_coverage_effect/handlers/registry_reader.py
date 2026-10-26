# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read published introspection manifests; discovery alone is never liveness."""

from __future__ import annotations

import httpx
from omnibase_core.models.contracts.model_topic_schema_binding import (
    parse_canonical_topic,
)
from omnibase_infra.runtime.auto_wiring.models.model_auto_wiring_manifest import (
    ModelAutoWiringManifest,
)

from omnimarket.models.runtime_coverage import ModelRuntimeRegistryEndpoint


async def read_registry(
    endpoint: ModelRuntimeRegistryEndpoint,
) -> frozenset[tuple[str, str]]:
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        response = await client.get(endpoint.manifest_url)
        response.raise_for_status()
        manifest = ModelAutoWiringManifest.model_validate(response.json())
    return frozenset(
        (contract.name, topic)
        for contract in manifest.contracts
        if contract.event_bus is not None
        for topic in contract.event_bus.subscribe_topics
        if parse_canonical_topic(topic).kind == "cmd"
    )
