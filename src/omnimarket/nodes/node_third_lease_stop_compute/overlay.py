# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``third_lease_stop`` overlay and the contract's configuration, joined into one config.

The contract holds the row grammar, the replay window and the row sources the node can read;
the overlay (``<root>/third_lease_stop/overlay.yaml``) holds the three deployment choices: the
lease count that stops, the mode and the row source. Every overlay key is required, there is no
packaged default, and an absent key refuses naming it.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib import resources
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_third_lease_stop_compute.models.model_third_lease_stop import (
    EnumThirdLeaseStopMode,
    ModelLeaseRowGrammar,
    ModelThirdLeaseStopConfig,
)

OVERLAY_ID = "third_lease_stop"
NODE_PACKAGE = "omnimarket.nodes.node_third_lease_stop_compute"


class ModelThirdLeaseStopOverlay(BaseModel):
    """``<root>/third_lease_stop/overlay.yaml``; every key is required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lease_count: int = Field(ge=2)
    mode: EnumThirdLeaseStopMode
    row_source: str = Field(min_length=1)


def load_contract_config() -> dict[str, Any]:
    """The ``config.third_lease_stop`` block of the packaged contract."""
    contract = yaml.safe_load(
        resources.files(NODE_PACKAGE).joinpath("contract.yaml").read_text()
    )
    block = contract["config"][OVERLAY_ID]
    if not isinstance(block, dict):
        raise NodeOverlayError(f"{OVERLAY_ID}: contract config is not a mapping")
    return block


def declared_row_sources() -> tuple[str, ...]:
    """The row source kinds the contract declares."""
    return tuple(load_contract_config()["row_sources"])


def bus_source() -> Mapping[str, str]:
    """The contract's bus row source: its ``topic`` and the message ``row_field``."""
    declared: Mapping[str, str] = load_contract_config()["row_sources"]["bus"]
    return declared


def load_third_lease_stop_config(
    environ: Mapping[str, str] | None = None, *, explicit: object | None = None
) -> ModelThirdLeaseStopConfig:
    """Join the contract and the overlay; refuses naming the key an absent or invalid overlay lacks."""
    block = load_contract_config()
    overlay = load_structured_overlay(
        OVERLAY_ID, ModelThirdLeaseStopOverlay, environ, explicit=explicit
    )
    if overlay.row_source not in block["row_sources"]:
        raise NodeOverlayError(
            f"{OVERLAY_ID}.row_source {overlay.row_source!r} is not a source the contract "
            f"declares: {sorted(block['row_sources'])}"
        )
    return ModelThirdLeaseStopConfig(
        stop_class_id=block["stop_class_id"],
        grammar=ModelLeaseRowGrammar(
            lease_row_type=block["lease_row"]["row_type"],
            cause_cell=block["lease_row"]["cause_cell"],
            lane_cell=block["lease_row"]["lane_cell"],
            lease_lane_prefixes=tuple(block["lease_row"]["lease_lane_prefixes"]),
            record_row_type=block["record_row"]["row_type"],
            record_cell=block["record_row"]["cause_cell"],
        ),
        replay_window_hours=block["replay_window_hours"],
        lease_count=overlay.lease_count,
        mode=overlay.mode,
        row_source=overlay.row_source,
    )
