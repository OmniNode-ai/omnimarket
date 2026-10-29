# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the engine ladder from the ``node_remote_lane_compute`` contract (OMN-21475).

Composition-time adapter: it owns the contract read, so the models and the pure handlers that
need the ladder take it validated. A missing or malformed ladder refuses; there is no fallback.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.models.model_engine_ladder import ModelEngineLadder

NODE = "node_remote_lane_compute"


def ladder_from_document(document: object, source: str) -> ModelEngineLadder:
    if not isinstance(document, dict):
        raise ValueError(f"{source}: contract must be a mapping")
    config = document.get("config")
    if not isinstance(config, dict) or "engine_ladder" not in config:
        raise ValueError(f"{source}: config.engine_ladder is not configured")
    return ModelEngineLadder.model_validate(
        {"entries": config["engine_ladder"]},
    )


@lru_cache(maxsize=1)
def _packaged() -> ModelEngineLadder:
    config = load_packaged_contract_section(NODE, "config")
    return ladder_from_document({"config": config}, f"{NODE}/contract.yaml")


def load_engine_ladder(contract: Path | None = None) -> ModelEngineLadder:
    """The ladder of the packaged contract, or of the contract file given (a fixture contract)."""
    if contract is None:
        return _packaged()
    return ladder_from_document(
        yaml.safe_load(contract.read_text(encoding="utf-8")), str(contract)
    )
