# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The PR claim registry's request, result and claim models.

Promoted out of node_pr_claim_registry_effect's models package (OMN-20912) so
node_github_repo_write_effect reads a claim with the registry's own types,
without reaching into that node (OMN-9263).
"""

from omnimarket.models.pr_claim.enum_pr_claim_operation import (
    EnumPrClaimOperation,
)
from omnimarket.models.pr_claim.model_pr_claim import ModelPrClaim
from omnimarket.models.pr_claim.model_pr_claim_registry_request import (
    ModelPrClaimRegistryRequest,
    require_valid_pr_key,
)
from omnimarket.models.pr_claim.model_pr_claim_registry_result import (
    ModelPrClaimRegistryResult,
)

__all__ = [
    "EnumPrClaimOperation",
    "ModelPrClaim",
    "ModelPrClaimRegistryRequest",
    "ModelPrClaimRegistryResult",
    "require_valid_pr_key",
]
