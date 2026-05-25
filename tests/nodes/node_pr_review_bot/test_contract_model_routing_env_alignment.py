# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract model_routing declares logical-key policy, not endpoint defaults.

PR review bot callers must provide reviewer/judge logical keys explicitly.
The contract owns the payload shape and resolution source, while endpoint and
served model IDs are resolved from policy/config at runtime.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_CONTRACT_PATH = (
    Path(__file__).parents[3] / "src/omnimarket/nodes/node_pr_review_bot/contract.yaml"
)

def test_contract_model_routing_declares_logical_key_policy_without_defaults() -> None:
    data = yaml.safe_load(_CONTRACT_PATH.read_text())
    model_routing: dict[str, object] = data.get("model_routing", {})

    assert (
        model_routing["reviewer"]["schema"] == "required_caller_supplied_logical_keys"
    )
    assert model_routing["reviewer"]["input_field"] == "reviewer_models"
    assert model_routing["reviewer"]["min_items"] == 1
    assert model_routing["judge"]["schema"] == "required_caller_supplied_logical_key"
    assert model_routing["judge"]["input_field"] == "judge_model"

    for role, declaration in model_routing.items():
        assert isinstance(declaration, dict), f"{role} declaration must be a dict"
        assert "primary" not in declaration
        assert "fallback" not in declaration
        assert "endpoint_env" not in declaration
        assert (
            declaration["resolution_source"]
            == "ModelInferenceBridgeConfig.model_configs"
        )


def test_contract_inputs_require_reviewer_and_judge_logical_keys() -> None:
    data = yaml.safe_load(_CONTRACT_PATH.read_text())
    inputs: dict[str, object] = data.get("inputs", {})

    reviewer_models = inputs["reviewer_models"]
    judge_model = inputs["judge_model"]

    assert reviewer_models["required"] is True
    assert "default" not in reviewer_models
    assert judge_model["required"] is True
    assert "default" not in judge_model
