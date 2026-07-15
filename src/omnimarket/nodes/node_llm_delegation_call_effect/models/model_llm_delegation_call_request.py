# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Re-export shim — canonical definition moved to omnimarket.events.llm_delegation_call (OMN-14586)."""

from omnimarket.events.llm_delegation_call import (
    ModelLlmDelegationCallRequest as ModelLlmDelegationCallRequest,
)

__all__ = ["ModelLlmDelegationCallRequest"]
