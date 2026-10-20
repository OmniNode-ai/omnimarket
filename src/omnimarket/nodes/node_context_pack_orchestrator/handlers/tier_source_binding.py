# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve contract-declared tier source handlers for plan task TA.8."""

from __future__ import annotations

import importlib
import importlib.resources
import importlib.util
from collections.abc import Mapping
from pathlib import Path

import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor

from omnimarket.nodes.node_context_pack_orchestrator.protocols import (
    ProtocolContextTierSource,
)

_SOURCE_INPUT_MODEL = "omnibase_core.models.context.model_context_source_request.ModelContextSourceRequest"


def _load_node_source(node_name: str) -> ProtocolContextTierSource:
    package = f"omnimarket.nodes.{node_name}"
    try:
        spec = importlib.util.find_spec(package)
    except (ImportError, ValueError) as exc:
        raise ValueError(f"Cannot find tier source node {node_name!r}") from exc
    if spec is None:
        raise ValueError(f"Cannot find tier source node {node_name!r}")
    try:
        contract = yaml.safe_load(
            importlib.resources.files(package)
            .joinpath("contract.yaml")
            .read_text(encoding="utf-8")
        )
    except (OSError, TypeError) as exc:
        raise ValueError(
            f"Cannot read contract for tier source node {node_name!r}"
        ) from exc
    if not isinstance(contract, Mapping):
        raise ValueError(f"Tier source node {node_name!r} contract must be a mapping")
    handler = contract.get("handler")
    if not isinstance(handler, Mapping):
        raise ValueError(f"Tier source node {node_name!r} must declare a handler")
    if (
        contract.get("input_model") != _SOURCE_INPUT_MODEL
        and handler.get("input_model") != _SOURCE_INPUT_MODEL
    ):
        raise ValueError(
            f"Tier source node {node_name!r} must accept {_SOURCE_INPUT_MODEL}"
        )
    module_name = handler.get("module")
    class_name = handler.get("class")
    if not isinstance(module_name, str) or not isinstance(class_name, str):
        raise ValueError(
            f"Tier source node {node_name!r} must declare handler module and class"
        )
    handler_class = getattr(importlib.import_module(module_name), class_name)
    instance = handler_class()
    if not isinstance(instance, ProtocolContextTierSource):
        raise ValueError(
            f"Tier source node {node_name!r} handler must implement ProtocolContextTierSource"
        )
    return instance


def load_tier_sources(
    contract_path: Path | None = None,
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
            nodes[node_name] = _load_node_source(node_name)
        sources[factor] = nodes[node_name]
    return sources
