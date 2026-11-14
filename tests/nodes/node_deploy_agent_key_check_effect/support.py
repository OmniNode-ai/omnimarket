# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders shared by the deploy agent key check effect tests (OMN-24413)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import yaml

from omnimarket.events.deploy_agent_key_check import (
    ModelDeployAgentKeyCheckRequest,
)
from omnimarket.nodes.node_deploy_agent_key_check_effect.overlay import (
    ModelDeployAgentKeyCheckOverlay,
)

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_deploy_agent_key_check_effect"
)
VECTOR = Path(__file__).parent / "key_check_vector.json"
RUN_ID = uuid4()
AGENT_KEY_ENV = "EXAMPLE_DEPLOY_AGENT_EXPECTED_KEY"
AGENT_KEY = "fixture-key"  # fixture value, not a credential


def overlay() -> ModelDeployAgentKeyCheckOverlay:
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    return ModelDeployAgentKeyCheckOverlay.model_validate(document)


def request() -> ModelDeployAgentKeyCheckRequest:
    return ModelDeployAgentKeyCheckRequest(correlation_id=RUN_ID, requested_by="user-a")
