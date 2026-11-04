# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads the review prompt-cache policy from the node contract (OMN-22343)."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.nodes.node_claude_review_gateway_effect.models.model_review_prompt_cache import (
    ModelReviewPromptCache,
)

_PACKAGE = "omnimarket.nodes.node_claude_review_gateway_effect"


def parse_prompt_cache(contract: Any) -> ModelReviewPromptCache:
    """The policy of a parsed ``contract.yaml``, refused when absent or invalid."""
    if not isinstance(contract, dict):
        raise ValueError("node_claude_review_gateway_effect contract is not a mapping")
    section = (contract.get("config") or {}).get("prompt_cache")
    if not isinstance(section, dict):
        raise ValueError(
            "node_claude_review_gateway_effect contract.yaml has no config.prompt_cache "
            "mapping: the cached blocks and their TTL are contract data, not code"
        )
    return ModelReviewPromptCache.model_validate(section)


@lru_cache(maxsize=1)
def load_prompt_cache() -> ModelReviewPromptCache:
    """The packaged contract's policy."""
    text = files(_PACKAGE).joinpath("contract.yaml").read_text("utf-8")
    return parse_prompt_cache(yaml.safe_load(text))


__all__ = ["load_prompt_cache", "parse_prompt_cache"]
