# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The orchestrator's leg config, read from its contract and overlays (OMN-23627).

Exemplar: ``nodes/node_pr_landing_orchestrator/orchestration/contract_config.py``.
Three declared sources, no default anywhere:

* this node's contract: ``completion_bound.max_wall_seconds`` and ``code_eval_config.max_hosts``;
* E2's contract: ``descriptor.timeout_ms``, the score's own deadline (plan rule 2);
* the node's overlay (:mod:`..overlay`) and E2's overlay (``ModelCodeScoreOverlay``).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_code_score import (
    ModelCodeScoreOverlay,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.leg import (
    CodeEvalLegConfig,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.overlay import (
    load_runner_overlay,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
SCORE_CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "node_code_contract_score_effect"
    / "contract.yaml"
)
SCORE_NODE_NAME = "node_code_contract_score_effect"


def _read(path: Path) -> dict[str, object]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a mapping")
    return raw


def completion_bound_seconds(contract: Mapping[str, object] | None = None) -> int:
    document = _read(CONTRACT_PATH) if contract is None else contract
    block = document.get("completion_bound")
    if not isinstance(block, dict) or not isinstance(
        block.get("max_wall_seconds"), int
    ):
        raise ValueError("the contract declares no completion_bound.max_wall_seconds")
    return int(block["max_wall_seconds"])


def max_hosts(contract: Mapping[str, object] | None = None) -> int:
    document = _read(CONTRACT_PATH) if contract is None else contract
    block = document.get("code_eval_config")
    if not isinstance(block, dict) or not isinstance(block.get("max_hosts"), int):
        raise ValueError("the contract declares no code_eval_config.max_hosts")
    return int(block["max_hosts"])


def score_timeout_seconds() -> int:
    descriptor = _read(SCORE_CONTRACT_PATH).get("descriptor")
    if not isinstance(descriptor, dict) or not isinstance(
        descriptor.get("timeout_ms"), int
    ):
        raise ValueError("the score node's contract declares no descriptor.timeout_ms")
    return int(descriptor["timeout_ms"]) // 1000


def load_leg_config(environ: Mapping[str, str] | None = None) -> CodeEvalLegConfig:
    """Read every value a leg needs; an absent one refuses with the key that is missing."""
    return CodeEvalLegConfig(
        overlay=load_runner_overlay(environ),
        score_overlay=load_structured_overlay(
            SCORE_NODE_NAME, ModelCodeScoreOverlay, environ
        ),
        completion_bound_seconds=completion_bound_seconds(),
        score_timeout_seconds=score_timeout_seconds(),
    )


__all__ = [
    "completion_bound_seconds",
    "load_leg_config",
    "max_hosts",
    "score_timeout_seconds",
]
