# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Dep-health coverage marker for handler_llm_delegation_call."""

from __future__ import annotations

from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    HandlerLlmDelegationCall,
)


def test_handler_llm_delegation_call_importable() -> None:
    assert HandlerLlmDelegationCall.__name__ == "HandlerLlmDelegationCall"
