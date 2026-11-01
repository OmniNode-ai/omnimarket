# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared config of the compute node's contract, read at the effect boundary."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_merged_running_count_compute.models.model_merged_running_count import (
    ModelMrcConfig,
)

COMPUTE_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "node_merged_running_count_compute"
    / "contract.yaml"
)
CONFIG_KEY = "merged_running_count"


def load_contract_config(contract_path: Path = COMPUTE_CONTRACT) -> ModelMrcConfig:
    """The ``merged_running_count`` block of the compute node's contract as a typed config."""
    document = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return ModelMrcConfig.model_validate(document[CONFIG_KEY])
