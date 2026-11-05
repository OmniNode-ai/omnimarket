# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read surface's topic, freshness threshold and wait pacing, from the contract."""

from pathlib import Path

import yaml

from omnimarket.models.pr_state.model_pr_state_read_policy import (
    ModelPrStateReadPolicy,
)

CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"


def load_pr_state_read_policy(
    contract_path: Path = CONTRACT_PATH,
) -> ModelPrStateReadPolicy:
    """Read the ``pr_state_read`` block of the node contract."""
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return ModelPrStateReadPolicy.model_validate(contract["pr_state_read"])
