# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21378: the no-local-model refusal reads the credential store before it speaks.

The refusal ended with "Your own provider key is missing too: no provider key is
registered here." as a literal, appended whatever the store held. A developer who
had just registered three keys read that their key was missing and was sent to
write a local-model overlay they did not need. The key half is now said only when
the store holds no key; with a key registered, the refusal names that key and the
absent route instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_infra.errors import ProtocolConfigurationError
from omnibase_infra.utils.util_error_sanitization import sanitize_error_message

from omnimarket.inference.local_byok_credential_adapter import (
    register_local_byok_credential,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
    refuse_undeclared_local_model,
)

pytestmark = pytest.mark.unit

_TENANT = "3314bdfb-85c8-45fc-a601-e31e6b9d9d93"
_HOUSE_REF = "llm.gemini.api_key"
_FAKE_KEY = "sk-test-" + "k" * 40
_BACKENDS = [
    {"backend_id": "local-coder", "tier": "local", "endpoint_url": None},
    {"backend_id": "local-heavy-reasoning", "tier": "local", "endpoint_url": None},
    {"backend_id": "cloud-gemini-flash", "tier": "cheap_cloud"},
]


@pytest.fixture(autouse=True)
def local_store_at_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    return db_path


def _house_rung() -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id="cloud-gemini-flash",
        model_id="gemini-3.5-flash-lite",
        endpoint_ref="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        tier="cheap_cloud",
        max_tokens=1024,
        timeout_ms=240000,
        secret_ref=_HOUSE_REF,
    )


def _refusal() -> str:
    with pytest.raises(ProtocolConfigurationError) as excinfo:
        refuse_undeclared_local_model(
            tenant_id=_TENANT,
            backend=_house_rung(),
            house_refs=frozenset({_HOUSE_REF}),
            backends=_BACKENDS,
        )
    return sanitize_error_message(excinfo.value)


def test_a_registered_key_is_not_reported_missing(local_store_at_tmp: Path) -> None:
    """AC1: with one key in the store, the key half is not said."""
    register_local_byok_credential("gemini", _FAKE_KEY, db_path=local_store_at_tmp)

    message = _refusal()

    assert "no provider key is registered here" not in message
    assert "provider key is missing" not in message


def test_a_registered_key_is_named_with_the_absent_route(
    local_store_at_tmp: Path,
) -> None:
    """AC2: the remediation names the registered key and the route, not the overlay file."""
    register_local_byok_credential("gemini", _FAKE_KEY, db_path=local_store_at_tmp)
    register_local_byok_credential("openai", _FAKE_KEY, db_path=local_store_at_tmp)

    message = _refusal()

    assert "gemini" in message
    assert "openai" in message
    assert "route" in message
    assert "bifrost_overrides.yaml" not in message
    assert "No local model is declared" not in message


def test_with_no_key_the_key_half_is_still_said() -> None:
    """The negative control: an empty store keeps the OMN-17373 sentence."""
    message = _refusal()

    assert "No local model is declared" in message
    assert "no provider key is registered here" in message


def test_a_key_that_failed_its_last_test_is_named_as_skipped(
    local_store_at_tmp: Path,
) -> None:
    """A registered key that delegation skips is named with the reason it is skipped."""
    import json

    from omnimarket.inference.local_byok_credential_adapter import (
        local_model_tests_path,
    )

    register_local_byok_credential("gemini", _FAKE_KEY, db_path=local_store_at_tmp)
    record = local_model_tests_path(local_store_at_tmp)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"gemini": {"status": "failed"}}), encoding="utf-8")

    message = _refusal()

    assert "failed its last test (gemini)" in message
    assert "onex models test" in message
    assert "bifrost_overrides.yaml" not in message
