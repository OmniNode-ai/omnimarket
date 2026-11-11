# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the bound batch policy; tests may supply an explicit policy."""

from omnimarket.handlers.container.container_overlay_dependencies import (
    installed_overlay_document,
)
from omnimarket.models.model_rule_premise import ModelBatchPolicy


def load_batch_policy() -> ModelBatchPolicy:
    document = installed_overlay_document("rule_premise")
    if document is None:
        raise ValueError("rule_premise policy overlay is unbound")
    return ModelBatchPolicy.model_validate(document["policy"])
