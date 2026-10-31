# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bounds the lab-fill local ports run with, read from the node contract (OMN-21623).

The ``port_timeouts`` block of ``node_lab_fill_effect``'s contract.yaml declares how
long each local port waits on the Linear query, the triage-fences CLI, the lane-rules
script, the claim bridge, the PR claim registry CLI and the ledger append receipt.
:func:`load_lab_fill_port_config` reads it through the shared contract block loader and
each port receives the result, so no timeout is held in the port's code.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.contract_block_loader import load_contract_block

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
CONFIG_KEY = "port_timeouts"


class ModelLabFillPortConfig(BaseModel):
    """Seconds each local port waits before it gives up."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    linear_query_timeout_seconds: float = Field(..., gt=0)
    triage_fences_timeout_seconds: float = Field(..., gt=0)
    lane_rules_block_timeout_seconds: float = Field(..., gt=0)
    claim_bridge_timeout_seconds: float = Field(..., gt=0)
    pr_claim_registry_timeout_seconds: float = Field(..., gt=0)
    status_append_timeout_seconds: float = Field(..., gt=0)


def load_lab_fill_port_config(
    contract_path: Path | None = None,
) -> ModelLabFillPortConfig:
    """Read and validate the ``port_timeouts`` block of the node's contract."""
    return load_contract_block(
        contract_path or CONTRACT_PATH, CONFIG_KEY, ModelLabFillPortConfig
    )


__all__ = [
    "CONFIG_KEY",
    "CONTRACT_PATH",
    "ModelLabFillPortConfig",
    "load_lab_fill_port_config",
]
