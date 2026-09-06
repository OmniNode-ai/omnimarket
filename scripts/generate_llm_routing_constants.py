# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Generate typed LLM routing constants from registry and topology contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.delegation.llm_cost_routing.model_llm_model_registry import (
    ModelLlmModelRegistryLoader,
)
from omnimarket.rsd.route_contract_bundle import route_contract_bundle_sha256

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_REGISTRY = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "data"
    / "model_registry"
    / "model_registry_v1.yaml"
)
_DEFAULT_OUTPUT = (
    _REPO_ROOT / "src" / "omnimarket" / "routing" / "generated_llm_routing_constants.py"
)
_DEFAULT_TOPOLOGY = (
    _REPO_ROOT.parent / "omnibase_infra" / "contracts" / "llm_endpoints.yaml"
)
_DEFAULT_ROUTING_TIERS = (
    _REPO_ROOT / "src" / "omnimarket" / "configs" / "routing_tiers.yaml"
)
_DEFAULT_BIFROST_DELEGATION = (
    _REPO_ROOT / "src" / "omnimarket" / "configs" / "bifrost_delegation.yaml"
)
_DEFAULT_ENDPOINT_REGISTRY = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_swarm_registry_compute"
    / "contracts"
    / "endpoint_registry.yaml"
)


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text()) or {}


def _constant_name(value: str) -> str:
    name = re.sub(r"[^0-9A-Za-z]+", "_", value).strip("_").upper()
    if not name:
        msg = f"Cannot generate constant name for empty value: {value!r}"
        raise ValueError(msg)
    if name[0].isdigit():
        name = f"MODEL_{name}"
    return name


def _string_literal(value: str) -> str:
    return json.dumps(value)


def _tuple_literal(values: list[str]) -> str:
    if len(values) == 1:
        return f"({_string_literal(values[0])},)"
    return "(" + ", ".join(_string_literal(value) for value in values) + ")"


def _wrapped_string_literal(value: str) -> str:
    """Render a generated long string in ruff's stable multiline form."""

    return f"(\n    {_string_literal(value)}\n)"


def _multiline_tuple_literal(values: list[str]) -> str:
    """Render a provenance tuple in ruff's stable multiline form."""

    return "(\n" + "".join(f"    {_string_literal(value)},\n" for value in values) + ")"


def _registry_values(registry_path: Path) -> tuple[dict[str, Any], list[str], set[str]]:
    registry = _load_yaml(registry_path)
    models = registry.get("models")
    if not isinstance(models, dict) or not models:
        msg = f"Model registry must contain non-empty models mapping: {registry_path}"
        raise ValueError(msg)
    model_keys = sorted(str(key) for key in models)
    endpoint_refs = {
        str(profile["endpoint_env"])
        for profile in models.values()
        if isinstance(profile, dict) and profile.get("endpoint_env")
    }
    return registry, model_keys, endpoint_refs


def _topology_endpoint_refs(topology_path: Path | None) -> set[str]:
    if topology_path is None or not topology_path.exists():
        return set()
    topology = _load_yaml(topology_path)
    endpoints = topology.get("endpoints")
    if not isinstance(endpoints, list):
        msg = f"Topology contract must contain endpoints list: {topology_path}"
        raise ValueError(msg)
    refs: set[str] = set()
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        for field in ("url_env_var", "role_env_alias"):
            value = endpoint.get(field)
            if value:
                refs.add(str(value))
    return refs


def _model_registry_hash(registry_path: Path) -> str:
    """Return the canonical model-registry hash used by the runtime DTO."""

    return ModelLlmModelRegistryLoader(registry_path).load().model_registry_hash


def _routing_tiers_hash(routing_tiers_path: Path) -> str:
    """Return SHA-256 over the exact packaged routing-contract bytes."""

    return hashlib.sha256(routing_tiers_path.read_bytes()).hexdigest()


def render_constants(
    *,
    registry_path: Path,
    topology_path: Path | None = None,
    routing_tiers_path: Path = _DEFAULT_ROUTING_TIERS,
    bifrost_delegation_path: Path = _DEFAULT_BIFROST_DELEGATION,
    endpoint_registry_path: Path = _DEFAULT_ENDPOINT_REGISTRY,
) -> str:
    registry, model_keys, registry_endpoint_refs = _registry_values(registry_path)
    endpoint_refs = sorted(
        registry_endpoint_refs | _topology_endpoint_refs(topology_path)
    )
    registry_version = str(registry.get("model_registry_version", ""))
    pricing_version = str(registry.get("pricing_manifest_version", ""))
    observed_at = str(registry.get("observed_at", ""))
    model_registry_hash = _model_registry_hash(registry_path)
    model_registry_raw_hash = _routing_tiers_hash(registry_path)
    routing_tiers_hash = _routing_tiers_hash(routing_tiers_path)
    bifrost_delegation_hash = _routing_tiers_hash(bifrost_delegation_path)
    endpoint_registry_hash = _routing_tiers_hash(endpoint_registry_path)
    route_contract_bundle_hash = route_contract_bundle_sha256(
        {
            "routing_tiers_raw_sha256": routing_tiers_hash,
            "bifrost_delegation_raw_sha256": bifrost_delegation_hash,
            "endpoint_registry_raw_sha256": endpoint_registry_hash,
            "model_registry_raw_sha256": model_registry_raw_hash,
            "model_registry_canonical_sha256": model_registry_hash,
        }
    )
    source_paths = [
        str(routing_tiers_path.resolve().relative_to(_REPO_ROOT)),
        str(bifrost_delegation_path.resolve().relative_to(_REPO_ROOT)),
        str(endpoint_registry_path.resolve().relative_to(_REPO_ROOT)),
        str(registry_path.resolve().relative_to(_REPO_ROOT)),
    ]
    if topology_path is not None and topology_path.exists():
        resolved_topology = topology_path.resolve()
        try:
            source_paths.append(str(resolved_topology.relative_to(_REPO_ROOT.parent)))
        except ValueError:
            source_paths.append(str(resolved_topology))

    model_lines = "\n".join(
        f"    {_constant_name(model_key)} = {_string_literal(model_key)}"
        for model_key in model_keys
    )
    endpoint_lines = "\n".join(
        f"    {_constant_name(endpoint_ref)} = {_string_literal(endpoint_ref)}"
        for endpoint_ref in endpoint_refs
    )

    return f'''# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Generated LLM routing constants.

Generated by scripts/generate_llm_routing_constants.py.
Do not edit by hand; update registry/topology contracts and regenerate.
"""

from __future__ import annotations

from enum import StrEnum, unique

MODEL_REGISTRY_VERSION = {_string_literal(registry_version)}
PRICING_MANIFEST_VERSION = {_string_literal(pricing_version)}
REGISTRY_OBSERVED_AT = {_string_literal(observed_at)}
MODEL_REGISTRY_SHA256 = {_wrapped_string_literal(model_registry_hash)}
MODEL_REGISTRY_RAW_SHA256 = {_wrapped_string_literal(model_registry_raw_hash)}
ROUTING_TIERS_SHA256 = {_wrapped_string_literal(routing_tiers_hash)}
BIFROST_DELEGATION_SHA256 = {_wrapped_string_literal(bifrost_delegation_hash)}
ENDPOINT_REGISTRY_SHA256 = {_wrapped_string_literal(endpoint_registry_hash)}
ROUTE_CONTRACT_BUNDLE_SHA256 = {_wrapped_string_literal(route_contract_bundle_hash)}
GENERATED_FROM = {_multiline_tuple_literal(source_paths)}


@unique
class EnumLogicalModelKey(StrEnum):
    """Logical model keys declared by the model registry."""

{model_lines}


@unique
class EnumLlmEndpointRef(StrEnum):
    """Endpoint reference names declared by registry/topology contracts."""

{endpoint_lines}


LOGICAL_MODEL_KEYS: tuple[str, ...] = tuple(item.value for item in EnumLogicalModelKey)
LLM_ENDPOINT_REFS: tuple[str, ...] = tuple(item.value for item in EnumLlmEndpointRef)

__all__ = [
    "BIFROST_DELEGATION_SHA256",
    "ENDPOINT_REGISTRY_SHA256",
    "GENERATED_FROM",
    "LLM_ENDPOINT_REFS",
    "LOGICAL_MODEL_KEYS",
    "MODEL_REGISTRY_RAW_SHA256",
    "MODEL_REGISTRY_SHA256",
    "MODEL_REGISTRY_VERSION",
    "PRICING_MANIFEST_VERSION",
    "REGISTRY_OBSERVED_AT",
    "ROUTE_CONTRACT_BUNDLE_SHA256",
    "ROUTING_TIERS_SHA256",
    "EnumLlmEndpointRef",
    "EnumLogicalModelKey",
]
'''


def write_constants(
    *,
    registry_path: Path,
    output_path: Path,
    topology_path: Path | None,
    routing_tiers_path: Path,
    bifrost_delegation_path: Path,
    endpoint_registry_path: Path,
    check: bool,
) -> int:
    rendered = render_constants(
        registry_path=registry_path,
        topology_path=topology_path,
        routing_tiers_path=routing_tiers_path,
        bifrost_delegation_path=bifrost_delegation_path,
        endpoint_registry_path=endpoint_registry_path,
    )
    if check:
        if not output_path.exists() or output_path.read_text() != rendered:
            print(f"{output_path} is stale; regenerate LLM routing constants")
            return 1
        print(f"{output_path} is up to date")
        return 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered)
    print(f"generated {output_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=_DEFAULT_REGISTRY)
    parser.add_argument("--topology", type=Path, default=_DEFAULT_TOPOLOGY)
    parser.add_argument("--routing-tiers", type=Path, default=_DEFAULT_ROUTING_TIERS)
    parser.add_argument(
        "--bifrost-delegation", type=Path, default=_DEFAULT_BIFROST_DELEGATION
    )
    parser.add_argument(
        "--endpoint-registry", type=Path, default=_DEFAULT_ENDPOINT_REGISTRY
    )
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    topology_path = args.topology if args.topology.exists() else None
    return write_constants(
        registry_path=args.registry,
        output_path=args.output,
        topology_path=topology_path,
        routing_tiers_path=args.routing_tiers,
        bifrost_delegation_path=args.bifrost_delegation,
        endpoint_registry_path=args.endpoint_registry,
        check=args.check,
    )


if __name__ == "__main__":
    raise SystemExit(main())
