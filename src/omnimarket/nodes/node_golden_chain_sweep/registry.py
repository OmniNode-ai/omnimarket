# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registry loader for golden chain definitions.

Reads chain definitions from golden_chains.yaml at startup.
Falls back to the provided default list if the file is missing or unreadable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from omnimarket.nodes.node_golden_chain_sweep.handlers.handler_golden_chain_sweep import (
    ModelChainDefinition,
)

_log = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).parent / "golden_chains.yaml"


@dataclass(frozen=True)
class ChainRegistryEntry:
    """Raw registry entry validated before conversion to ModelChainDefinition."""

    name: str
    head_topic: str
    tail_table: str
    expected_fields: list[str] = field(default_factory=list)

    def to_model(self) -> ModelChainDefinition:
        return ModelChainDefinition(
            name=self.name,
            head_topic=self.head_topic,
            tail_table=self.tail_table,
            expected_fields=self.expected_fields,
        )


def load_registry(
    path: Path | None = None,
    fallback: list[ModelChainDefinition] | None = None,
) -> list[ModelChainDefinition]:
    """Load chain definitions from a YAML registry file.

    Falls back to `fallback` if the file is missing or invalid.
    """
    registry_path = path if path is not None else _REGISTRY_PATH
    default: list[ModelChainDefinition] = fallback if fallback is not None else []

    if not registry_path.exists():
        _log.warning(
            "golden_chains.yaml not found at %s — using fallback", registry_path
        )
        return default

    try:
        raw = yaml.safe_load(registry_path.read_text())
    except Exception as exc:
        _log.warning("failed to read %s: %s — using fallback", registry_path, exc)
        return default

    if not isinstance(raw, dict) or "chains" not in raw:
        _log.warning("golden_chains.yaml missing 'chains' key — using fallback")
        return default

    chains: list[ModelChainDefinition] = []
    for item in raw["chains"]:
        try:
            entry = ChainRegistryEntry(
                name=item["name"],
                head_topic=item["head_topic"],
                tail_table=item["tail_table"],
                expected_fields=item.get("expected_fields", []),
            )
            chains.append(entry.to_model())
        except (KeyError, TypeError) as exc:
            _log.warning("skipping malformed chain entry %r: %s", item, exc)

    if not chains:
        _log.warning("golden_chains.yaml produced no valid chains — using fallback")
        return default

    return chains
