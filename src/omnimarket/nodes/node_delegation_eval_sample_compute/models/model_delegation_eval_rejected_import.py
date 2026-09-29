# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Imported identity that cannot enter the sample."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegation_eval_sample_compute.models.enum_delegation_eval_import_rejection import (
    EnumDelegationEvalImportRejection,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_key import (
    ModelDelegationEvalKey,
)


class ModelDelegationEvalRejectedImport(BaseModel):
    """Imported identity that cannot enter the sample."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: ModelDelegationEvalKey
    reason: EnumDelegationEvalImportRejection
