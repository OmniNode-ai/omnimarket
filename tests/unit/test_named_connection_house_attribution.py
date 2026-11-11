# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A customer connection must not become a platform key through its overlay."""

from types import SimpleNamespace

from omnimarket.handlers.routing.customer_key_terminus import house_credential_refs
from omnimarket.inference import local_byok_credential_adapter as credentials


def test_only_registered_named_credential_is_excluded_from_house_set(
    tmp_path, monkeypatch
):
    db = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(credentials, "default_evidence_db_path", lambda: db)
    reference = credentials.register_named_connection(
        "customer",
        "openai_compatible",
        "https://customer.example",
        ("dynamic",),
        "private-key",
        db_path=db,
    )
    backends = {
        "customer": SimpleNamespace(api_key_ref=reference, api_key_env=None),
        "forged": SimpleNamespace(
            api_key_ref="cred_localinstall_openai_compatible_not_registered",
            api_key_env=None,
        ),
        "platform": SimpleNamespace(
            api_key_ref="llm.platform.api_key", api_key_env=None
        ),
    }
    refs = house_credential_refs(backends)
    assert reference not in refs
    assert "llm.platform.api_key" in refs
    assert "cred_localinstall_openai_compatible_not_registered" in refs
    credentials.remove_named_connection("customer", db_path=db)
    assert reference in house_credential_refs(backends)


def test_named_byok_requires_matching_canonical_binding(tmp_path, monkeypatch):
    from omnimarket.handlers.routing.delegation_backend_resolution import (
        ModelResolvedDelegationBackend,
    )
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
        _is_customer_byok_route,
    )
    from omnimarket.nodes.node_model_setup_effect.models.model_endpoint_connection import (
        ModelEndpointConnection,
    )

    db = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(credentials, "default_evidence_db_path", lambda: db)
    reference = credentials.register_named_connection(
        "customer",
        "openai_compatible",
        "https://customer.example",
        ("dynamic",),
        "private-key",
        db_path=db,
    )
    connection = ModelEndpointConnection.model_validate(
        credentials.read_named_connections(db_path=db)[0]
    )
    route = ModelResolvedDelegationBackend(
        backend_id=connection.backend_id("dynamic"),
        model_id="dynamic",
        endpoint_ref="https://customer.example/v1/chat/completions",
        tier="cheap_cloud",
        max_tokens=1024,
        timeout_ms=1000,
        secret_ref=reference,
    )
    assert _is_customer_byok_route(route)
    assert not _is_customer_byok_route(
        route.model_copy(
            update={"endpoint_ref": "https://other.example/v1/chat/completions"}
        )
    )
    assert not _is_customer_byok_route(
        route.model_copy(update={"secret_ref": "llm.platform.api_key"})
    )
