# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve contract-declared tier source handlers for plan task TA.8."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.nodes.node_context_pack_orchestrator.protocols import (
    ProtocolContextTierSource,
)


def load_tier_sources(
    contract_path: Path | None = None,
    *,
    resolve: Callable[[str], ProtocolContextTierSource] | None = None,
) -> dict[EnumContextFactor, ProtocolContextTierSource]:
    """Load factor bindings, sharing one handler instance per source node name."""
    path = (
        contract_path
        if contract_path is not None
        else Path(__file__).resolve().parents[1] / "contract.yaml"
    )
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(contract, Mapping):
        raise ValueError("Tier source contract must be a mapping")
    block = contract.get("context_tier_sources", {})
    if not isinstance(block, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in block.items()
    ):
        raise ValueError("context_tier_sources must be a mapping of str to str")
    sources: dict[EnumContextFactor, ProtocolContextTierSource] = {}
    nodes: dict[str, ProtocolContextTierSource] = {}
    for key, node_name in block.items():
        try:
            factor = EnumContextFactor(key)
        except ValueError as exc:
            raise ValueError(f"Unknown context tier factor {key!r}") from exc
        if node_name not in nodes:
            if resolve is None:
                raise ValueError(
                    f"Tier source {node_name!r} requires a runtime-injected port"
                )
            source = resolve(node_name)
            if not isinstance(source, ProtocolContextTierSource):
                raise ValueError(
                    f"Tier source {node_name!r} must implement ProtocolContextTierSource"
                )
            nodes[node_name] = source
        sources[factor] = nodes[node_name]
    return sources
