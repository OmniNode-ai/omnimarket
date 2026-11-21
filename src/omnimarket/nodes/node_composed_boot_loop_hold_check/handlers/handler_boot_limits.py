# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the limits the dev kernel runs under from the declarations that own them, and render the compose override.

The memory limit is read from the runtime policy contract (the main runtime's boot memory limit) and the CPU cap from the
runtime base anchor of the infra compose file, both in the infra checkout the composed-runtime workflow already holds.
The two bounds are read from this node's own contract. Nothing here has a default: a missing key is an error that names
the file and the key, never a fallback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_composed_boot_loop_hold_check.models.model_boot_bounds import (
    ModelBootBounds,
)

_COMPOSE_TAG_PREFIXES = ("!override", "!reset")


class LimitSourceError(ValueError):
    """A declaration the limits are read from is absent or malformed."""


def _walk(document: Any, path: tuple[str, ...], source: str) -> Any:
    node = document
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise LimitSourceError(f"{source}: key {'.'.join(path)} is not declared")
        node = node[key]
    return node


def _load_compose(text: str) -> dict[str, Any]:
    for tag in _COMPOSE_TAG_PREFIXES:
        text = text.replace(f" {tag} ", " ")
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise LimitSourceError("compose file is not a mapping")
    return loaded


def read_node_declaration(contract_path: Path) -> dict[str, Any]:
    """The ``boot_check`` block of this node's contract."""
    loaded = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    block = _walk(loaded, ("boot_check",), str(contract_path))
    if not isinstance(block, dict):
        raise LimitSourceError(f"{contract_path}: boot_check is not a mapping")
    return block


def resolve_bounds(contract_path: Path, infra_root: Path) -> ModelBootBounds:
    """Bounds from the node contract, limits from the dev declarations named in it."""
    declaration = read_node_declaration(contract_path)
    sources = _walk(declaration, ("limit_sources",), str(contract_path))

    policy_spec = _walk(sources, ("memory_limit",), str(contract_path))
    policy_file = infra_root / str(_walk(policy_spec, ("file",), str(contract_path)))
    policy = yaml.safe_load(policy_file.read_text(encoding="utf-8"))
    memory = _walk(policy, tuple(policy_spec["key_path"]), str(policy_file))

    cpu_spec = _walk(sources, ("cpus",), str(contract_path))
    compose_file = infra_root / str(_walk(cpu_spec, ("file",), str(contract_path)))
    compose = _load_compose(compose_file.read_text(encoding="utf-8"))
    cpus = _walk(compose, tuple(cpu_spec["key_path"]), str(compose_file))

    return ModelBootBounds(
        loop_hold_bound_seconds=float(
            _walk(declaration, ("loop_hold_bound_seconds",), str(contract_path))
        ),
        health_bound_seconds=float(
            _walk(declaration, ("health_bound_seconds",), str(contract_path))
        ),
        cpus=float(cpus),
        memory_limit=str(memory),
    )


_MEMORY_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def memory_bytes(text: str) -> int:
    """Bytes of a compose memory string such as ``3072M`` or ``16g``."""
    stripped = text.strip().lower().removesuffix("b")
    digits = stripped.rstrip("kmg")
    unit = stripped[len(digits) :]
    if not digits.isdigit() or unit not in _MEMORY_UNITS:
        raise LimitSourceError(f"memory limit {text!r} is not a compose memory string")
    return int(digits) * _MEMORY_UNITS[unit]


def limits_applied_error(
    bounds: ModelBootBounds, nano_cpus: int, memory: int
) -> str | None:
    """Why a container's applied limits are not the declared ones, or None when they match."""
    expected_nano = round(bounds.cpus * 1_000_000_000)
    if nano_cpus != expected_nano:
        return f"container runs with NanoCpus={nano_cpus}, declared {expected_nano} (cpus {bounds.cpus:g})"
    expected_memory = memory_bytes(bounds.memory_limit)
    if memory != expected_memory:
        return f"container runs with Memory={memory}, declared {expected_memory} ({bounds.memory_limit})"
    return None


def render_override(
    bounds: ModelBootBounds,
    services: tuple[str, ...],
    sampler_dir: str,
    sampler_out: str,
    existing_pythonpath: str = "",
) -> dict[str, Any]:
    """Compose override that applies the limits to the kernel services and loads the stack sampler into them."""
    body: dict[str, Any] = {
        "environment": {
            "PYTHONPATH": f"{sampler_dir}:{existing_pythonpath}"
            if existing_pythonpath
            else sampler_dir,
            "ONEX_BOOT_SAMPLER_OUT": sampler_out,
            "ONEX_BOOT_SAMPLER_BOUND_SECONDS": str(bounds.loop_hold_bound_seconds),
        },
        "volumes": [f"{sampler_dir}:{sampler_dir}:ro"],
        "deploy": {
            "resources": {
                "limits": {"cpus": str(bounds.cpus), "memory": bounds.memory_limit}
            }
        },
    }
    return {"services": {name: dict(body) for name in services}}
