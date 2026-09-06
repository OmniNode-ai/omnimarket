# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical, domain-separated provenance digest for the offline C0 bundle."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

ROUTE_CONTRACT_BUNDLE_DOMAIN = "omnimarket.routing.bundle.v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMPONENT_NAMES = (
    "routing_tiers_raw_sha256",
    "bifrost_delegation_raw_sha256",
    "endpoint_registry_raw_sha256",
    "model_registry_raw_sha256",
    "model_registry_canonical_sha256",
)
_SOURCE_PATHS = {
    "routing_tiers_raw_sha256": "src/omnimarket/configs/routing_tiers.yaml",
    "bifrost_delegation_raw_sha256": "src/omnimarket/configs/bifrost_delegation.yaml",
    "endpoint_registry_raw_sha256": "src/omnimarket/nodes/node_swarm_registry_compute/contracts/endpoint_registry.yaml",
    "model_registry_raw_sha256": "src/omnimarket/data/model_registry/model_registry_v1.yaml",
}


def route_contract_bundle_bytes(component_pins: Mapping[str, str]) -> bytes:
    """Frame exact named component digests into the versioned C0 bundle payload."""

    if set(component_pins) != set(_COMPONENT_NAMES):
        raise ValueError("route-contract bundle components do not match v1")
    for name in _COMPONENT_NAMES:
        digest = component_pins[name]
        if type(digest) is not str or _SHA256_PATTERN.fullmatch(digest) is None:
            raise ValueError("route-contract bundle component is not a SHA-256 digest")
    sources = [
        {
            "path": path,
            "role": name.removesuffix("_raw_sha256"),
            "sha256": component_pins[name],
        }
        for name, path in _SOURCE_PATHS.items()
    ]
    sources.sort(key=lambda item: (item["role"], item["path"]))
    return json.dumps(
        {
            "domain": ROUTE_CONTRACT_BUNDLE_DOMAIN,
            "model_registry_canonical_sha256": component_pins[
                "model_registry_canonical_sha256"
            ],
            "sources": sources,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def route_contract_bundle_sha256(component_pins: Mapping[str, str]) -> str:
    """Return the SHA-256 digest of the explicitly framed v1 bundle payload."""

    return hashlib.sha256(route_contract_bundle_bytes(component_pins)).hexdigest()


__all__ = [
    "ROUTE_CONTRACT_BUNDLE_DOMAIN",
    "route_contract_bundle_bytes",
    "route_contract_bundle_sha256",
]
