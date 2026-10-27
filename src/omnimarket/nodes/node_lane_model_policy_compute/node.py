# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical local entry point for the declared lane-model policy."""

from .handlers.handler_lane_model_policy import CONTRACT_PATH, HandlerLaneModelPolicy


class NodeLaneModelPolicyCompute(HandlerLaneModelPolicy):
    contract_path = CONTRACT_PATH
