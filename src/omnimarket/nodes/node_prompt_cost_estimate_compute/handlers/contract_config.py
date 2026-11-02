# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the estimate's typed blocks from its own contract, with the overlay laid over.

The CSV import schema comes from the contract alone, because the page reads the
same declaration. The saving range comes from the contract unless a deployment
overlay, ``<root>/prompt_cost_estimate/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS``, replaces it. The handler takes a provider so a
test, or a deployment, injects its own.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.nodes.node_prompt_cost_estimate_compute.models import (
    ModelPromptCostEstimateConfig,
    ModelPromptCostEstimateOverlay,
    ModelPromptCostEstimateTopics,
)

OVERLAY_ID = "prompt_cost_estimate"
_PACKAGE = "omnimarket.nodes.node_prompt_cost_estimate_compute"

ConfigProvider = Callable[[], ModelPromptCostEstimateConfig]


def load_contract() -> dict[str, Any]:
    """The packaged contract as a mapping."""
    raw = yaml.safe_load(files(_PACKAGE).joinpath("contract.yaml").read_text("utf-8"))
    if not isinstance(raw, dict):
        raise NodeOverlayError(f"{_PACKAGE} contract.yaml is not a mapping")
    return raw


def config_from_contract(
    contract: Mapping[str, Any], environ: Mapping[str, str] | None = None
) -> ModelPromptCostEstimateConfig:
    """The contract's ``config`` block, typed, with the overlay's saving range over it."""
    config = ModelPromptCostEstimateConfig.model_validate(contract["config"])
    if find_node_overlay(OVERLAY_ID, environ) is None:
        return config
    overlay = load_structured_overlay(
        OVERLAY_ID, ModelPromptCostEstimateOverlay, environ
    )
    if overlay.saving_range is None:
        return config
    return config.model_copy(update={"saving_range": overlay.saving_range})


def load_config(
    environ: Mapping[str, str] | None = None,
) -> ModelPromptCostEstimateConfig:
    """The default provider: this node's own contract and the overlay, if any."""
    return config_from_contract(load_contract(), environ)


def load_topics() -> ModelPromptCostEstimateTopics:
    """The command and result topics the contract declares."""
    contract = load_contract()
    return ModelPromptCostEstimateTopics(
        command=contract["runtime_dispatch"]["command_topic"],
        completed=contract["terminal_event"],
    )
