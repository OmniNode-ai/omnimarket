# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25856: the BYOK intake writes with its own writer identity.

On dev the runtime process carries one Infisical machine identity whose role is
read-only on the tenant-credential folder (it serves reads). The intake built its
store from that same identity, so every register answered 403 on create and the
route answered 503. The intake now takes a dedicated writer identity from its own
variables; with none set the process identity is used unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest
from omnibase_infra.adapters._internal import adapter_infisical as _adapter_module

import omnimarket.projection.credential_publisher as cp
from omnimarket.projection.credential_publisher import (
    CredentialStoreConfigurationError,
    _build_secret_store,
)

_BASE = {
    "INFISICAL_ADDR": "http://infisical.test:8880",
    "INFISICAL_CLIENT_ID": "read-only-client-id",
    "INFISICAL_CLIENT_SECRET": "read-only-client-secret",
    "INFISICAL_PROJECT_ID": "872e00f4-1279-49a4-8e16-a2bd961f4996",
    "INFISICAL_ENVIRONMENT_SLUG": "dev",
}
_WRITER_ID = "writer-id-value-9f3"
_WRITER_SECRET = "writer-secret-value-9f3"


class _StubAdapter:
    instances: list[_StubAdapter] = []

    def __init__(self, config: Any) -> None:
        self.config = config
        self.is_authenticated = False
        _StubAdapter.instances.append(self)

    async def initialize(self) -> None:
        self.is_authenticated = True

    async def shutdown(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    _StubAdapter.instances = []
    for name, value in _BASE.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(cp.WRITER_CLIENT_ID_VAR, raising=False)
    monkeypatch.delenv(cp.WRITER_CLIENT_SECRET_VAR, raising=False)
    monkeypatch.setattr(_adapter_module, "AdapterInfisical", _StubAdapter)


def _identity() -> tuple[str, str]:
    config = _StubAdapter.instances[0].config
    return config.client_id.get_secret_value(), config.client_secret.get_secret_value()


def test_writer_identity_is_used_for_the_write_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(cp.WRITER_CLIENT_ID_VAR, _WRITER_ID)
    monkeypatch.setenv(cp.WRITER_CLIENT_SECRET_VAR, _WRITER_SECRET)
    _build_secret_store()
    assert _identity() == (_WRITER_ID, _WRITER_SECRET)


def test_no_writer_identity_keeps_the_process_identity() -> None:
    _build_secret_store()
    assert _identity() == ("read-only-client-id", "read-only-client-secret")


def test_half_a_writer_identity_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(cp.WRITER_CLIENT_ID_VAR, _WRITER_ID)
    with pytest.raises(CredentialStoreConfigurationError) as excinfo:
        cp._bootstrap_values()
    assert cp.WRITER_CLIENT_SECRET_VAR in str(excinfo.value)
    assert _WRITER_ID not in str(excinfo.value)
