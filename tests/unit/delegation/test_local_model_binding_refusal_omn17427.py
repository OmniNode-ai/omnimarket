# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An executable local URL with no served-model binding is a config error."""

from types import SimpleNamespace

import pytest
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)


def test_missing_local_model_binding_is_not_silently_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = ModelDelegationBackendConfig(
        backend_id="local-coder",
        provider="local",
        tier="local",
        endpoint_url="https://local.test/v1/chat/completions",
        model_name=None,
    )
    cloud = ModelDelegationBackendConfig(
        backend_id="cloud",
        provider="openrouter",
        tier="cheap_frontier",
        endpoint_url="https://cloud.test/v1/chat/completions",
        model_name="cloud-model",
    )
    monkeypatch.setattr(
        routing,
        "load_bifrost_delegation_config",
        lambda **_: SimpleNamespace(backends=(missing, cloud)),
    )
    routing._load_bifrost_endpoints.cache_clear()
    try:
        with pytest.raises(
            ProtocolConfigurationError,
            match=r"local_model_binding_missing.*local-coder",
        ):
            routing._load_bifrost_endpoints()
    finally:
        routing._load_bifrost_endpoints.cache_clear()
