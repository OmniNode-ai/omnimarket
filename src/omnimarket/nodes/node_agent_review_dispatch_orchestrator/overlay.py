# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required deployment configuration and topics declared by the packaged contract."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)

from .models import ModelAgentReviewConfig, ModelAgentReviewTopics

OVERLAY_ID = "node_agent_review_dispatch_orchestrator"
_PACKAGE = "omnimarket.nodes.node_agent_review_dispatch_orchestrator"


def load_agent_review_config(
    environ: Mapping[str, str] | None = None,
) -> ModelAgentReviewConfig:
    """Load the required overlay, refusing absent or invalid deployment settings."""
    return load_structured_overlay(OVERLAY_ID, ModelAgentReviewConfig, environ)


def load_topics() -> ModelAgentReviewTopics:
    """Read command and terminal topics from the contract."""
    contract = yaml.safe_load(
        files(_PACKAGE).joinpath("contract.yaml").read_text("utf-8")
    )
    if not isinstance(contract, dict):
        raise NodeOverlayError(f"{OVERLAY_ID} contract.yaml is not a mapping")
    bus = contract["event_bus"]
    terminals = {
        outcome: next(
            topic
            for topic in bus["publish_topics"]
            if topic.endswith(f"agent-review-{outcome}.v1")
        )
        for outcome in ("completed", "refused", "failed")
    }
    return ModelAgentReviewTopics(command=bus["subscribe_topics"][0], **terminals)
