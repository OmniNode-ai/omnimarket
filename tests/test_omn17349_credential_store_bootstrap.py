# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""BYOK WRITE half: the intake store must actually be able to write (OMN-17349).

``credential_publisher._build_secret_store()`` is the ONLY place the real
Infisical-backed store is constructed, and it is the branch the deployed
onex-api route always takes (the route never passes ``secret_store=``). Every
pre-existing test injected a fake store, so the construction site itself had
zero coverage -- which is exactly how it shipped constructing an
``AdapterInfisical`` and never calling ``initialize()``, making every customer
BYOK write a deterministic 503.

These tests exercise ``_build_secret_store()`` itself against a stubbed adapter.
Injecting a fake ``secret_store`` cannot satisfy them (AC2), and the roundtrip
case drives the WRITE half into the merged OMN-16984 READ half so the two are
proven to agree on one ref.

The last section goes one layer deeper: it replaces only ``InfisicalSDKClient``
(the HTTP surface) and drives the REAL ``AdapterInfisical`` + REAL
``InfisicalSecretStore``, so the production guard at
``adapter_infisical.py:423``/``:481`` is the actual thing under test. That
section reproduces the live 503 verbatim on the pre-fix construction and proves
the fixed construction writes and reads back the same value.
"""

from __future__ import annotations

import json
import logging
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import infisical_sdk
import pytest
from omnibase_infra.adapters._internal import adapter_infisical as _adapter_module
from omnibase_infra.adapters._internal.adapter_infisical import AdapterInfisical
from omnibase_infra.adapters.models.model_infisical_config import (
    ModelInfisicalAdapterConfig,
)
from omnibase_infra.errors import InfraConnectionError
from omnibase_infra.errors import SecretResolutionError as InfraSecretResolutionError
from omnibase_infra.secret_stores.infisical_secret_store import InfisicalSecretStore
from pydantic import SecretStr, ValidationError

import omnimarket.projection.credential_publisher as cp
from omnimarket.inference.secret_store_resolver import (
    SecretResolutionError,
    resolve_tenant_scoped_api_key_async,
)
from omnimarket.projection.credential_publisher import (
    CredentialStoreConfigurationError,
    CredentialStoreUnavailableError,
    ModelInferenceCredentialCreateRequest,
    _build_secret_store,
    register_inference_credential,
)

pytestmark = pytest.mark.unit

# onex-allow-test-fixture OMN-17349 reason="synthetic values asserted to never
# appear in an error message, a log record, an event payload or a response;
# none is a real credential"
_CLIENT_ID = "ci-0000-not-a-real-client-id"
_CLIENT_SECRET = "cs-0000-not-a-real-client-secret"
_PROJECT_ID = "6f9619ff-8b86-d011-b42d-00cf4fc964ff"
_HOST = "http://infisical.invalid:8080"
_SLUG = "dev"
_PATH = "/tenant-inference-credentials"
_KEY_VALUE = "sk-or-v1-synthetic-byok-value-do-not-leak"

_BOOTSTRAP_ENV = {
    "INFISICAL_ADDR": _HOST,
    "INFISICAL_CLIENT_ID": _CLIENT_ID,
    "INFISICAL_CLIENT_SECRET": _CLIENT_SECRET,
    "INFISICAL_PROJECT_ID": _PROJECT_ID,
    "INFISICAL_ENVIRONMENT_SLUG": _SLUG,
    "INFISICAL_TENANT_CREDENTIAL_SECRET_PATH": _PATH,
}


class _StubAdapter:
    """Records lifecycle calls; never touches a network or the Infisical SDK."""

    instances: list[_StubAdapter] = []

    def __init__(self, config: Any, *, fail_initialize: bool = False) -> None:
        self.config = config
        self.initialize_calls = 0
        self.shutdown_calls = 0
        self._authenticated = False
        self._fail_initialize = fail_initialize
        type(self).instances.append(self)

    def initialize(self) -> None:
        self.initialize_calls += 1
        if self._fail_initialize:
            raise InfraConnectionError("Failed to initialize Infisical client: 401")
        self._authenticated = True

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self._authenticated = False


class _FailingStubAdapter(_StubAdapter):
    def __init__(self, config: Any) -> None:
        super().__init__(config, fail_initialize=True)


class _RecordingStore:
    """In-memory ``ProtocolSecretStore`` that records its own lifecycle."""

    def __init__(self, *, fail_set: bool = False, decline_set: bool = False) -> None:
        self.values: dict[str, str] = {}
        self.close_calls = 0
        self._fail_set = fail_set
        self._decline_set = decline_set

    async def get_secret(self, key: str) -> str | None:
        return self.values.get(key)

    async def set_secret(self, key: str, value: str) -> bool:
        if self._fail_set:
            raise RuntimeError("store write failed")
        if self._decline_set:
            # Protocol-legal refusal: "False otherwise", no exception.
            return False
        self.values[key] = value
        return True

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("read-only")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        return [k for k in self.values if prefix is None or k.startswith(prefix)]

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds
        self.close_calls += 1


@pytest.fixture(autouse=True)
def _reset_stub_instances() -> None:
    _StubAdapter.instances = []


@pytest.fixture
def bootstrap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _BOOTSTRAP_ENV.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def stub_adapter(monkeypatch: pytest.MonkeyPatch) -> type[_StubAdapter]:
    monkeypatch.setattr(_adapter_module, "AdapterInfisical", _StubAdapter)
    return _StubAdapter


def _fake_bus() -> AsyncMock:
    """Spec-bound event-bus double (transport-mock-lint, OMN-13026)."""
    return AsyncMock(spec=cp.ProtocolCredentialEventBus)


def _make_request(**overrides: object) -> ModelInferenceCredentialCreateRequest:
    fields: dict[str, object] = {
        "name": "my-openrouter-key",
        "provider": "openrouter",
        "key_value": _KEY_VALUE,
    }
    fields.update(overrides)
    return ModelInferenceCredentialCreateRequest(**fields)


# --------------------------------------------------------------------------
# AC1 / AC2 -- the construction site initializes the adapter
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("bootstrap_env")
async def test_build_secret_store_initializes_the_adapter(
    stub_adapter: type[_StubAdapter],
) -> None:
    """AC2: fails the moment ``initialize()`` is dropped again."""
    store = _build_secret_store()

    assert len(stub_adapter.instances) == 1
    adapter = stub_adapter.instances[0]
    assert adapter.initialize_calls == 1
    # Behavioural half of the same assertion: InfisicalSecretStore.health_check
    # returns adapter.is_authenticated, which is False until initialize() runs.
    assert await store.health_check() is True


@pytest.mark.usefixtures("bootstrap_env")
async def test_build_secret_store_carries_the_lane_addressing(
    stub_adapter: type[_StubAdapter],
) -> None:
    _build_secret_store()
    config = stub_adapter.instances[0].config
    assert config.host == _HOST
    assert str(config.project_id) == _PROJECT_ID
    assert config.environment_slug == _SLUG
    assert config.secret_path == _PATH


# --------------------------------------------------------------------------
# AC5 -- fail closed on an unset environment slug (never default to prod)
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("bootstrap_env")
def test_unset_environment_slug_fails_closed_instead_of_defaulting_to_prod(
    monkeypatch: pytest.MonkeyPatch, stub_adapter: type[_StubAdapter]
) -> None:
    monkeypatch.delenv("INFISICAL_ENVIRONMENT_SLUG", raising=False)

    with pytest.raises(CredentialStoreConfigurationError) as excinfo:
        _build_secret_store()

    message = str(excinfo.value)
    assert "INFISICAL_ENVIRONMENT_SLUG" in message
    # The dangerous default must not survive anywhere in the failure path.
    assert "prod" not in message
    assert stub_adapter.instances == [], (
        "no adapter may be constructed against an unresolved environment slug"
    )


# --------------------------------------------------------------------------
# AC1 -- missing/blank bootstrap config is a typed error, not a bare KeyError
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("bootstrap_env", "stub_adapter")
def test_missing_bootstrap_variables_are_named_together_not_raised_as_keyerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("INFISICAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("INFISICAL_PROJECT_ID", raising=False)

    with pytest.raises(CredentialStoreConfigurationError) as excinfo:
        _build_secret_store()

    message = str(excinfo.value)
    assert "INFISICAL_CLIENT_ID" in message
    assert "INFISICAL_PROJECT_ID" in message
    assert not isinstance(excinfo.value, KeyError)


@pytest.mark.usefixtures("bootstrap_env", "stub_adapter")
def test_blank_bootstrap_variable_is_treated_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INFISICAL_CLIENT_SECRET", "   ")

    with pytest.raises(CredentialStoreConfigurationError) as excinfo:
        _build_secret_store()

    assert "INFISICAL_CLIENT_SECRET" in str(excinfo.value)


@pytest.mark.usefixtures("bootstrap_env", "stub_adapter")
def test_malformed_project_id_is_a_typed_error_not_a_validation_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "not-a-uuid")

    with pytest.raises(CredentialStoreConfigurationError) as excinfo:
        _build_secret_store()

    assert "INFISICAL_PROJECT_ID" in str(excinfo.value)


# --------------------------------------------------------------------------
# AC1 -- a failed initialize() surfaces structurally, naming addressing only
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("bootstrap_env")
def test_failed_initialize_raises_typed_error_naming_host_project_and_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _adapter_module,
        "AdapterInfisical",
        _FailingStubAdapter,
    )

    with pytest.raises(CredentialStoreUnavailableError) as excinfo:
        _build_secret_store()

    message = str(excinfo.value)
    assert _HOST in message
    assert _PROJECT_ID in message
    assert _PATH in message
    assert _SLUG in message
    # Addressing only -- never identity material.
    assert _CLIENT_SECRET not in message
    assert _CLIENT_ID not in message
    assert isinstance(excinfo.value.__cause__, InfraConnectionError)


@pytest.mark.usefixtures("bootstrap_env")
def test_failed_initialize_releases_the_adapter_rather_than_leaking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _adapter_module,
        "AdapterInfisical",
        _FailingStubAdapter,
    )

    with pytest.raises(CredentialStoreUnavailableError):
        _build_secret_store()

    assert _StubAdapter.instances[0].shutdown_calls == 1


@pytest.mark.usefixtures("bootstrap_env")
def test_bootstrap_never_logs_identity_material(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        _adapter_module,
        "AdapterInfisical",
        _FailingStubAdapter,
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(CredentialStoreUnavailableError):
        _build_secret_store()

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert _CLIENT_SECRET not in blob
    assert _CLIENT_ID not in blob


# --------------------------------------------------------------------------
# AC3 -- the constructed store's lifetime is owned and closed
# --------------------------------------------------------------------------


async def test_register_closes_the_store_it_constructed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _RecordingStore()
    monkeypatch.setattr(cp, "_build_secret_store", lambda: store)

    await register_inference_credential(
        _make_request(), tenant_id="t-alpha", event_bus=_fake_bus()
    )

    assert store.close_calls == 1


async def test_register_closes_the_constructed_store_when_set_secret_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _RecordingStore(fail_set=True)
    monkeypatch.setattr(cp, "_build_secret_store", lambda: store)

    with pytest.raises(RuntimeError):
        await register_inference_credential(
            _make_request(), tenant_id="t-alpha", event_bus=_fake_bus()
        )

    assert store.close_calls == 1


async def test_register_never_closes_an_injected_store() -> None:
    """The caller that supplies a store owns its lifetime."""
    store = _RecordingStore()

    await register_inference_credential(
        _make_request(),
        tenant_id="t-alpha",
        secret_store=store,
        event_bus=_fake_bus(),
    )

    assert store.close_calls == 0


async def test_a_declined_write_is_never_reported_as_a_registered_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``set_secret -> False`` must fail the request, not publish a phantom ref.

    ``ProtocolSecretStore.set_secret`` is declared ``-> bool`` and documents
    "False otherwise", so a conforming store may decline without raising.
    Discarding that boolean would 201 the customer with an ``api_key_ref`` the
    store does not hold -- a credential that looks live in the dashboard and
    fails to resolve at every delegation. The store is still closed.
    """
    store = _RecordingStore(decline_set=True)
    monkeypatch.setattr(cp, "_build_secret_store", lambda: store)
    bus = _fake_bus()

    with pytest.raises(cp.CredentialStoreWriteRejectedError) as excinfo:
        await register_inference_credential(
            _make_request(), tenant_id="t-alpha", event_bus=bus
        )

    message = str(excinfo.value)
    assert "t-alpha" in message
    assert _KEY_VALUE not in message
    bus.publish_envelope.assert_not_awaited()
    assert store.values == {}
    assert store.close_calls == 1


async def test_a_declined_write_on_an_injected_store_also_refuses() -> None:
    """Same refusal on the injected-store branch, and no close of a store we
    do not own -- the two ownership branches must not diverge on the sad path."""
    store = _RecordingStore(decline_set=True)
    bus = _fake_bus()

    with pytest.raises(cp.CredentialStoreWriteRejectedError):
        await register_inference_credential(
            _make_request(), tenant_id="t-alpha", secret_store=store, event_bus=bus
        )

    bus.publish_envelope.assert_not_awaited()
    assert store.close_calls == 0


# --------------------------------------------------------------------------
# Negative paths -- tenant scoping and secret containment
# --------------------------------------------------------------------------


def test_create_request_cannot_carry_a_caller_supplied_ref() -> None:
    """A tenant cannot address (and therefore cannot overwrite) another's ref."""
    with pytest.raises(ValidationError):
        _make_request(api_key_ref="cred_t-beta_openrouter_" + "0" * 32)


async def test_minted_ref_carries_only_the_authenticated_tenant() -> None:
    store = _RecordingStore()

    alpha = await register_inference_credential(
        _make_request(), tenant_id="t-alpha", secret_store=store, event_bus=_fake_bus()
    )
    beta = await register_inference_credential(
        _make_request(), tenant_id="t-beta", secret_store=store, event_bus=_fake_bus()
    )

    assert alpha.api_key_ref.startswith("cred_t-alpha_")
    assert beta.api_key_ref.startswith("cred_t-beta_")
    assert alpha.api_key_ref != beta.api_key_ref


async def test_write_then_read_roundtrip_and_the_other_tenants_ref_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WRITE half feeds the merged OMN-16984 READ half over one shared store.

    Also the negative half: a ref minted for a tenant that never registered a
    value resolves to nothing -- it never picks up the other tenant's value and
    never falls back to a house key, even with one present in the environment.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "house-key-must-never-be-used")
    store = _RecordingStore()

    registered = await register_inference_credential(
        _make_request(), tenant_id="t-alpha", secret_store=store, event_bus=_fake_bus()
    )

    resolved = await resolve_tenant_scoped_api_key_async(
        registered.api_key_ref, tenant_id="t-alpha", store=store
    )
    assert resolved is not None
    assert resolved.get_secret_value() == _KEY_VALUE

    unregistered = cp.mint_api_key_ref("t-beta", "openrouter")
    with pytest.raises(SecretResolutionError) as excinfo:
        await resolve_tenant_scoped_api_key_async(
            unregistered, tenant_id="t-beta", store=store
        )
    assert "t-beta" in str(excinfo.value)
    assert _KEY_VALUE not in str(excinfo.value)
    assert "house-key-must-never-be-used" not in str(excinfo.value)


async def test_only_the_ref_ever_leaves_the_write_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OMN-17352 / U1, write side: nothing durable carries the value.

    The event payload is what the projection persists and the response is what
    the customer's browser gets; neither may carry the key. This is the write
    half of the at-rest question -- the direct store readback belongs to
    OMN-17352 itself.
    """
    store = _RecordingStore()
    bus = _fake_bus()

    with caplog.at_level(logging.DEBUG):
        response = await register_inference_credential(
            _make_request(),
            tenant_id="t-alpha",
            secret_store=store,
            event_bus=bus,
        )

    envelope = bus.publish_envelope.await_args.args[0]
    payload_json = envelope.payload.model_dump_json()
    assert _KEY_VALUE not in payload_json
    assert response.api_key_ref in payload_json

    assert _KEY_VALUE not in json.dumps(response.model_dump(mode="json"))
    assert _KEY_VALUE not in "\n".join(r.getMessage() for r in caplog.records)

    # The value did reach the store -- the one and only place it belongs.
    assert store.values[response.api_key_ref] == _KEY_VALUE


# --------------------------------------------------------------------------
# Integration: the REAL adapter + REAL store, only the SDK's HTTP client faked
# --------------------------------------------------------------------------


class _FakeInfisicalSecrets:
    """The secret CRUD surface of the Infisical SDK, backed by a dict."""

    def __init__(self, server: _FakeInfisicalServer) -> None:
        self._server = server

    def create_secret_by_name(
        self, *, secret_name: str, secret_value: str, **addressing: object
    ) -> SimpleNamespace:
        self._server.record("create", secret_name, addressing)
        self._server.secrets[secret_name] = secret_value
        return SimpleNamespace(secretKey=secret_name, version=1)

    def update_secret_by_name(
        self, *, current_secret_name: str, secret_value: str, **addressing: object
    ) -> SimpleNamespace:
        self._server.record("update", current_secret_name, addressing)
        if current_secret_name not in self._server.secrets:
            # What a real Infisical returns for a not-yet-existing secret; the
            # adapter wraps it in InfraConnectionError and InfisicalSecretStore
            # falls back to create_secret. That fallback is the reason the live
            # 503 traceback showed BOTH write paths failing.
            raise LookupError("secret not found")
        self._server.secrets[current_secret_name] = secret_value
        return SimpleNamespace(secretKey=current_secret_name, version=2)

    def get_secret_by_name(
        self, *, secret_name: str, **addressing: object
    ) -> SimpleNamespace:
        self._server.record("get", secret_name, addressing)
        if secret_name not in self._server.secrets:
            raise LookupError("secret not found")
        return SimpleNamespace(
            secretKey=secret_name,
            secretValue=self._server.secrets[secret_name],
            version=1,
        )


class _FakeInfisicalServer:
    """Minimal stand-in for the Infisical HTTP API.

    Only ``InfisicalSDKClient`` is replaced. ``AdapterInfisical`` (its auth
    guard, its kwarg shapes, its error wrapping) and ``InfisicalSecretStore``
    (its update-then-create fallback, its ``to_thread`` bridging) are the real
    classes under test.
    """

    def __init__(self, *, accept_login: bool = True) -> None:
        self.secrets: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.logins: int = 0
        self._accept_login = accept_login

    def record(self, op: str, name: str, addressing: dict[str, object]) -> None:
        self.calls.append((op, name, dict(addressing)))

    def client_factory(self, *, host: str) -> SimpleNamespace:
        self.host = host
        server = self

        class _UniversalAuth:
            def login(self, *, client_id: str, client_secret: str) -> object:
                server.logins += 1
                if not server._accept_login:
                    raise RuntimeError("401 Unauthorized")
                assert client_id == _CLIENT_ID
                assert client_secret == _CLIENT_SECRET
                return SimpleNamespace(accessToken="fake-access-token")

        return SimpleNamespace(
            auth=SimpleNamespace(universal_auth=_UniversalAuth()),
            secrets=_FakeInfisicalSecrets(server),
        )


@pytest.fixture
def infisical_server(monkeypatch: pytest.MonkeyPatch) -> _FakeInfisicalServer:
    server = _FakeInfisicalServer()
    monkeypatch.setattr(infisical_sdk, "InfisicalSDKClient", server.client_factory)
    return server


@pytest.mark.usefixtures("bootstrap_env")
async def test_uninitialized_adapter_reproduces_the_live_503_verbatim() -> None:
    """The defect, pinned: the pre-fix construction cannot write, ever.

    This is the 2026-08-31 staging repro rebuilt in-process --
    ``SecretResolutionError: Infisical adapter not initialized. Call
    initialize() first.`` out of BOTH write paths, which the onex-api route
    maps to HTTP 503. No SDK is needed to reach it: the guard fires before any
    network call, which is why it was deterministic for every tenant.
    """
    config = ModelInfisicalAdapterConfig(
        host=_HOST,
        client_id=SecretStr(_CLIENT_ID),
        client_secret=SecretStr(_CLIENT_SECRET),
        project_id=uuid.UUID(_PROJECT_ID),
        environment_slug=_SLUG,
        secret_path=_PATH,
    )
    unfixed_store = InfisicalSecretStore(
        AdapterInfisical(config),
        project_id=_PROJECT_ID,
        environment_slug=_SLUG,
        secret_path=_PATH,
    )

    with pytest.raises(InfraSecretResolutionError) as excinfo:
        await unfixed_store.set_secret(
            "cred_t-alpha_openrouter_" + "0" * 32, _KEY_VALUE
        )

    assert "Infisical adapter not initialized" in str(excinfo.value)


@pytest.mark.usefixtures("bootstrap_env")
async def test_fixed_builder_writes_and_reads_back_through_the_real_adapter(
    infisical_server: _FakeInfisicalServer,
) -> None:
    """WRITE half -> READ half over one store, all real layers but the socket.

    Injected into ``register_inference_credential`` deliberately: the store is
    the one ``_build_secret_store()`` produced, and injecting it keeps it open
    past the call so the READ half can use the same authenticated adapter (the
    owned-store close is asserted separately).
    """
    store = _build_secret_store()
    assert infisical_server.logins == 1

    response = await register_inference_credential(
        _make_request(),
        tenant_id="t-alpha",
        secret_store=store,
        event_bus=_fake_bus(),
    )

    # The value landed at the minted ref, in the declared env and folder.
    assert infisical_server.secrets[response.api_key_ref] == _KEY_VALUE
    write = next(c for c in infisical_server.calls if c[0] == "create")
    assert write[1] == response.api_key_ref
    assert write[2]["environment_slug"] == _SLUG
    assert write[2]["secret_path"] == _PATH
    assert write[2]["project_id"] == _PROJECT_ID

    # READ half (OMN-16984, merged omnimarket#2220) resolves the same ref.
    resolved = await resolve_tenant_scoped_api_key_async(
        response.api_key_ref, tenant_id="t-alpha", store=store
    )
    assert resolved is not None
    assert resolved.get_secret_value() == _KEY_VALUE

    # A ref the store never wrote stays unresolvable -- no house-key fallback.
    with pytest.raises(SecretResolutionError):
        await resolve_tenant_scoped_api_key_async(
            cp.mint_api_key_ref("t-beta", "openrouter"), tenant_id="t-beta", store=store
        )

    await store.close()


@pytest.mark.usefixtures("bootstrap_env")
async def test_real_adapter_auth_failure_is_caught_as_store_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the except clause matches what the REAL adapter actually raises.

    A stubbed adapter can only prove the handler catches what the stub throws.
    This drives ``AdapterInfisical.initialize()`` itself into a login failure,
    so the ``InfraConnectionError`` being caught is the real one.
    """
    server = _FakeInfisicalServer(accept_login=False)
    monkeypatch.setattr(infisical_sdk, "InfisicalSDKClient", server.client_factory)

    with pytest.raises(CredentialStoreUnavailableError) as excinfo:
        _build_secret_store()

    assert server.logins == 1
    assert _CLIENT_SECRET not in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, InfraConnectionError)
