# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node uses the shared overlay document model and keeps no node-local copy."""

from __future__ import annotations

import importlib.util

from omnimarket.models.overlay_documents.model_tenant_api_key_overlay import (
    ModelTenantApiKeyOverlay as SharedOverlay,
)
from omnimarket.nodes.node_tenant_api_key_effect.handlers import (
    handler_tenant_api_key_register as register_module,
)
from tests.nodes.node_tenant_api_key_effect import support


def test_handler_and_support_use_the_shared_overlay_model() -> None:
    assert register_module.ModelTenantApiKeyOverlay is SharedOverlay
    assert support.ModelTenantApiKeyOverlay is SharedOverlay


def test_node_keeps_no_local_overlay_model() -> None:
    assert (
        importlib.util.find_spec(
            "omnimarket.nodes.node_tenant_api_key_effect.models.model_tenant_api_key_overlay"
        )
        is None
    )
