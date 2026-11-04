# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure lane rules rendering and path decisions from caller-supplied facts."""

from __future__ import annotations

from pathlib import PurePosixPath

from omnimarket.nodes.node_lane_rules_block_compute.models.model_lane_rules_block import (
    ModelLaneRulesBlockRequest,
    ModelLaneRulesBlockResult,
)

OMNI_PLUGIN_PLACEHOLDER = "<omni " + "plugin>"
OMNI_PLUGIN_HANDLE = '"$OMNI_PLUGIN_ROOT"'
CONTRACT_STEP = (
    "Before the first push, write contracts/{ticket}.yaml with a dod_evidence entry per "
    "acceptance criterion and, where deploy-gate applies, a falsifiable deploy check scoped "
    "so the hosted runner can run it; the push is refused without it."
)


class HandlerLaneRulesBlock:
    """Stateless renderer of lane rules, contract instructions and block paths."""

    def handle(self, request: ModelLaneRulesBlockRequest) -> ModelLaneRulesBlockResult:
        if request.operation == "render_block":
            return ModelLaneRulesBlockResult(
                operation=request.operation,
                text=request.block_text.replace(
                    OMNI_PLUGIN_PLACEHOLDER, OMNI_PLUGIN_HANDLE
                ),
            )
        if request.operation in ("contract_step", "contract_block"):
            text = CONTRACT_STEP.format(ticket=request.ticket)
            if request.operation == "contract_block":
                text = (
                    "Required when this lane may open or push a PR in a repository whose CI runs "
                    "repo-evidence / dod-verify or deploy-gate (read its .github/workflows CI config):\n"
                    + text
                )
            return ModelLaneRulesBlockResult(operation=request.operation, text=text)
        if request.resolve_error is not None:
            return ModelLaneRulesBlockResult(
                operation=request.operation, exit_code=2, message=request.resolve_error
            )
        assert request.checkout_path is not None
        block = str(
            PurePosixPath(request.checkout_path) / "lane_rules" / "lane-rules-block.md"
        )
        if not request.block_exists:
            return ModelLaneRulesBlockResult(
                operation=request.operation,
                exit_code=2,
                message=f"lane rules block is missing: {block}",
            )
        return ModelLaneRulesBlockResult(operation=request.operation, path=block)
