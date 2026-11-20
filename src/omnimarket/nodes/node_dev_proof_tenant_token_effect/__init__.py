# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_dev_proof_tenant_token_effect: signs the two dev proof tenant tokens and seeds them into the dev secret store."""

from omnimarket.nodes.node_dev_proof_tenant_token_effect.handlers.handler_dev_proof_tenant_token import (
    HandlerDevProofTenantToken,
)

__all__ = ["HandlerDevProofTenantToken"]
