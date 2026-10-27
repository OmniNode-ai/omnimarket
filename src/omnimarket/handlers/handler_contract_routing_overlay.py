# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a node's explicit packaged routing overlay at the composition boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def routing_overlay_path(contract_path: Path, contract: dict[str, Any]) -> Path:
    """Resolve a sibling YAML resource; never search another location or default."""
    reference = contract["model_routing_overlay"]
    if (
        not isinstance(reference, str)
        or not reference
        or Path(reference).name != reference
    ):
        raise ValueError("model_routing_overlay must name a sibling YAML resource")
    overlay_path = contract_path.parent / reference
    if overlay_path.resolve().parent != contract_path.parent.resolve():
        raise ValueError("model_routing_overlay escapes the node package")
    if "model_routing" in contract:
        raise ValueError(
            "both inline model_routing and model_routing_overlay are declared"
        )
    return overlay_path


def load_routing_contract(contract_path: Path) -> dict[str, Any]:
    """Compose routing for runtime use; preserve contracts without routing overlays."""
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(contract, dict):
        raise ValueError("contract must be a mapping")
    if "model_routing_overlay" not in contract:
        return contract
    overlay_path = routing_overlay_path(contract_path, contract)
    overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    if not isinstance(overlay, dict) or not isinstance(
        overlay.get("model_routing"), dict
    ):
        raise ValueError("routing overlay must declare a model_routing mapping")
    return {**contract, "model_routing": overlay["model_routing"]}
