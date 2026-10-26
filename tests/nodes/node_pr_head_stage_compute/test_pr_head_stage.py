# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: node_pr_head_stage_compute renders the remote lane's PR-head fetch and stage shell."""

from __future__ import annotations

import pytest
import yaml

from omnimarket.nodes.node_pr_head_stage_compute import NodePrHeadStageCompute
from omnimarket.nodes.node_pr_head_stage_compute.models.model_pr_head_stage import (
    ModelPrHead,
    ModelPrHeadStageRequest,
)

pytestmark = pytest.mark.unit

SHA = "a" * 40
HEADS = (
    ModelPrHead("omnimarket#4137", "omnimarket", "topic/pr", SHA),
    ModelPrHead("other#9", "other", "x", "b" * 40),
)


def test_contract_names_the_node_and_its_models() -> None:
    contract = yaml.safe_load(NodePrHeadStageCompute.contract_path.read_text())
    assert contract["name"] == "node_pr_head_stage_compute"
    assert contract["execution"] == {
        "deterministic": True,
        "side_effects": False,
        "caller": "remote-lane runner PR-head fetch and stage steps",
    }
    assert contract["entrypoint"]["name"] == NodePrHeadStageCompute.__name__


def test_stage_script_copies_only_this_repository_heads_over_local_transport() -> None:
    result = NodePrHeadStageCompute().handle(
        ModelPrHeadStageRequest(
            HEADS, "OmniNode-ai", "export PATH=x", "/omni", "omnimarket", "rlane-x"
        )
    )
    stage = result.stage_script
    assert 'C="$LR/runs/rlane-x.ctx"' in stage
    assert "K=/omni/omnimarket" in stage
    assert f'--no-write-fetch-head -q "$S" {SHA}' in stage
    assert "b" * 40 not in stage
    assert "github.com" not in stage
    assert (
        f"printf \"REMOTE-LANE-PR-HEAD-STAGED %s %s %s\\n\" 'omnimarket#4137' {SHA}"
        in stage
    )
    # The fetch step still renders both heads, from the host clone or the cache.
    assert result.fetch_script.startswith("export PATH=x\n")
    assert (
        result.fetch_script.count("REMOTE-LANE-PR-HEAD %s") == 4
    )  # two lines per head


def test_stage_script_is_empty_without_a_repository_or_run() -> None:
    assert (
        NodePrHeadStageCompute()
        .handle(ModelPrHeadStageRequest(HEADS, "OmniNode-ai", ""))
        .stage_script
        == ""
    )
