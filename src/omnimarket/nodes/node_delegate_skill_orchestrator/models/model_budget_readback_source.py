# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the AC3 readback finds delegation runs: a lab projection (OMN-21326)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


class ModelBudgetReadbackSource(BaseModel):
    """The contract-declared relation and the environment variable naming its DSN.

    The host, port and credentials are never in the contract: the overlay or the
    secret store sets the variable on whichever lab host runs the readback.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    relation: str = Field(pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
    dsn_env: str = Field(min_length=1)
    binding_ref: str = Field(min_length=1)
    populated_by: str = Field(min_length=1)


def load_budget_readback_source(
    contract_path: Path = _CONTRACT,
) -> ModelBudgetReadbackSource:
    """Load and validate ``budget_readback.source`` from the node contract."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    block = raw.get("budget_readback") if isinstance(raw, dict) else None
    source = block.get("source") if isinstance(block, dict) else None
    if not isinstance(source, dict):
        raise ValueError("budget_readback.source must be a mapping in the contract")
    return ModelBudgetReadbackSource.model_validate(source)


__all__ = ["ModelBudgetReadbackSource", "load_budget_readback_source"]
