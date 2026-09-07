# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16944 AC4: the BYOK write half and read half must address one secret.

The two halves of the BYOK credential plane derive their Infisical address in
different repos, from different declarations, and never call each other:

* WRITE -- ``omnimarket.projection.credential_publisher`` roots its store at
  ``INFISICAL_TENANT_CREDENTIAL_SECRET_PATH`` (defaulting to the product-level
  tenant-credential folder) and calls ``set_secret(minted_ref, value)``, so the
  value lands at ``<folder>/<minted_ref>``;
* READ -- the lane's ``ONEX_SECRET_RESOLVER_CONFIG_*`` declares a
  ``ModelSecretNamespaceRule`` whose ``source_path_template`` is interpolated
  with the ref and split back into (folder, name) by
  ``SecretResolver._split_infisical_path``.

Nothing structural forces those two to agree. If they diverge, the customer's
POST succeeds, the projection records the ref, and the first keyed delegation
fails as though the tenant never registered a key. This chain writes with the
real publisher and reads with the real resolver over ONE store, so a divergence
fails here rather than on a deployed lane.

The Infisical client is replaced at its two construction seams only; every
addressing decision under test is the shipped code's own. No secret VALUE is
asserted into any message.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from omnimarket.inference import secret_store_resolver as ssr
from omnimarket.inference.secret_store_resolver import (
    SecretResolutionError,
    clear_secret_store_resolver_cache,
    resolve_api_key_async,
)
from omnimarket.projection import credential_publisher as cp
from omnimarket.projection.credential_publisher import (
    ModelInferenceCredentialCreateRequest,
    register_inference_credential,
)

pytestmark = [pytest.mark.integration]

_INFISICAL_BOOTSTRAP_VARS = (
    "INFISICAL_ADDR",
    "INFISICAL_CLIENT_ID",
    "INFISICAL_CLIENT_SECRET",
    "INFISICAL_PROJECT_ID",
    "INFISICAL_ENVIRONMENT_SLUG",
)
_PROJECT_UUID = "e5010c63-94b3-43ed-9554-0d2dcf3c4e36"
_TENANT_ID = "af432b87"
# onex-allow-test-fixture OMN-16944 reason="synthetic BYOK key literal round-tripped through a hermetic in-memory store; never a real credential"
_SUBMITTED_VALUE = "sk-or-v1-omn16944-synthetic-round-trip-value"


class _HermeticInfisicalStore:
    """An Infisical-shaped store: every secret is addressed (folder, name).

    Mirrors ``InfisicalSecretStore``'s contract at the one property that
    matters here -- a store constructed for a ``secret_path`` writes and reads
    flat names UNDER that path. The backing dict is shared with the read half's
    handler, so the folder each half computes is the only thing that can make
    them miss each other.
    """

    def __init__(self, secrets: dict[str, str], *, secret_path: str) -> None:
        self._secrets = secrets
        self._secret_path = secret_path
        self.closed = False

    def _address(self, key: str) -> str:
        return f"{self._secret_path.rstrip('/')}/{key}"

    async def get_secret(self, key: str) -> str | None:
        return self._secrets.get(self._address(key))

    async def set_secret(self, key: str, value: str) -> bool:
        self._secrets[self._address(key)] = value
        return True

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("read-only, per OMN-2286")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        del prefix
        return sorted(self._secrets)

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds
        self.closed = True


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def start(self) -> None:
        return

    async def close(self) -> None:
        return

    async def publish_envelope(
        self, envelope: Any, topic: str, key: bytes | None = None
    ) -> None:
        del key
        self.published.append((topic, envelope))


class _FakeInfisicalHandler:
    """Read-half stand-in for ``HandlerInfisical``; addresses (folder, name)."""

    def __init__(self, secrets: dict[str, str], *, secret_path: str) -> None:
        self._secrets = secrets
        self.secret_path = secret_path
        self.reads: list[tuple[str, str | None]] = []

    def _lookup(self, secret_name: str, folder: str | None) -> str | None:
        effective = folder if folder is not None else self.secret_path
        return self._secrets.get(f"{effective.rstrip('/')}/{secret_name}")

    def get_secret_sync(
        self,
        *,
        secret_name: str,
        project_id: str | None = None,
        environment_slug: str | None = None,
        secret_path: str | None = None,
    ) -> SecretStr | None:
        self.reads.append((secret_name, secret_path))
        value = self._lookup(secret_name, secret_path)
        return SecretStr(value) if value is not None else None

    async def execute(self, envelope: dict[str, Any]) -> Any:
        payload = envelope["payload"]
        secret_name = payload["secret_name"]
        folder = payload.get("secret_path")
        self.reads.append((secret_name, folder))
        value = self._lookup(secret_name, folder)
        return SimpleNamespace(result={"value": value} if value is not None else {})


@pytest.fixture(autouse=True)
def _isolated_lane(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name in (
        *_INFISICAL_BOOTSTRAP_VARS,
        "INFISICAL_REQUIRED",
        "INFISICAL_TENANT_CREDENTIAL_SECRET_PATH",
        "ONEX_SECRET_RESOLVER_CONFIG_PATH",
        "ONEX_SECRET_RESOLVER_CONFIG_JSON",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("INFISICAL_ADDR", "http://infisical.example.invalid:8080")
    monkeypatch.setenv("INFISICAL_CLIENT_ID", "id-not-a-secret-value")
    monkeypatch.setenv("INFISICAL_CLIENT_SECRET", "unit-test-placeholder")
    monkeypatch.setenv("INFISICAL_PROJECT_ID", _PROJECT_UUID)
    monkeypatch.setenv("INFISICAL_ENVIRONMENT_SLUG", "dev")
    clear_secret_store_resolver_cache()
    yield
    clear_secret_store_resolver_cache()


def _write_folder() -> str:
    """The folder the WRITE half will root its store at -- its own derivation."""
    return cp._bootstrap_values()["INFISICAL_TENANT_CREDENTIAL_SECRET_PATH"]


def _declare_read_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, template_folder: str
) -> None:
    """Declare the lane exactly as k8s/onex-dev/runtime/configmap.yaml does."""
    config_file = tmp_path / "secret_resolver.yaml"
    config_file.write_text(
        textwrap.dedent(f"""\
            enable_convention_fallback: false
            mappings: []
            namespaces:
              - namespace: tenant_inference_credentials
                ref_pattern: '^cred_[A-Za-z0-9._:-]+_[A-Za-z0-9_-]+_[0-9a-f]{{32}}$'
                source_type: infisical
                source_path_template: {template_folder}/{{ref}}
        """),
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", str(config_file))
    clear_secret_store_resolver_cache()


async def _register(store: _HermeticInfisicalStore) -> str:
    response = await register_inference_credential(
        ModelInferenceCredentialCreateRequest(
            name="omn16944-round-trip",
            provider="openrouter",
            key_value=_SUBMITTED_VALUE,
        ),
        tenant_id=_TENANT_ID,
        secret_store=store,
        event_bus=_RecordingBus(),
    )
    return response.api_key_ref


class TestWriteAndReadAgreeOnOneAddress:
    async def test_registered_credential_resolves_through_the_namespace_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secrets: dict[str, str] = {}
        write_folder = _write_folder()
        store = _HermeticInfisicalStore(secrets, secret_path=write_folder)

        minted_ref = await _register(store)

        # The write half put it exactly one place, and the projection/customer
        # only ever learn the ref.
        assert list(secrets) == [f"{write_folder}/{minted_ref}"]

        _declare_read_lane(tmp_path, monkeypatch, template_folder=write_folder)
        handler: dict[str, Any] = {}

        def _fake_build(config: Any) -> _FakeInfisicalHandler:
            handler["h"] = _FakeInfisicalHandler(
                secrets, secret_path=config.secret_path
            )
            return handler["h"]

        monkeypatch.setattr(ssr, "_build_infisical_handler", _fake_build)

        resolved = await resolve_api_key_async(minted_ref)

        assert isinstance(resolved, SecretStr)
        assert resolved.get_secret_value() == _SUBMITTED_VALUE
        # The read addressed the folder the WRITE half chose, per-read -- not a
        # handler default (this lane declares no Infisical mapping at all).
        assert handler["h"].reads == [(minted_ref, write_folder)]

    async def test_a_divergent_read_folder_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control: the assertion above is capable of failing.

        Same write, same store, read lane pointed one folder over. The chain
        must miss and RAISE -- never find the value anyway (which would prove
        the round trip above was not actually addressing anything).
        """
        secrets: dict[str, str] = {}
        write_folder = _write_folder()
        store = _HermeticInfisicalStore(secrets, secret_path=write_folder)

        minted_ref = await _register(store)

        _declare_read_lane(tmp_path, monkeypatch, template_folder="/some-other-folder")
        monkeypatch.setattr(
            ssr,
            "_build_infisical_handler",
            lambda config: _FakeInfisicalHandler(
                secrets, secret_path=config.secret_path
            ),
        )

        with pytest.raises(SecretResolutionError) as excinfo:
            await resolve_api_key_async(minted_ref)

        assert _SUBMITTED_VALUE not in str(excinfo.value)
        assert _TENANT_ID in str(excinfo.value)

    async def test_write_folder_override_is_honoured_by_both_halves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lane may move the folder; the two halves must move together."""
        monkeypatch.setenv(
            "INFISICAL_TENANT_CREDENTIAL_SECRET_PATH", "/relocated-tenant-credentials"
        )
        secrets: dict[str, str] = {}
        write_folder = _write_folder()
        assert write_folder == "/relocated-tenant-credentials"

        store = _HermeticInfisicalStore(secrets, secret_path=write_folder)
        minted_ref = await _register(store)

        _declare_read_lane(tmp_path, monkeypatch, template_folder=write_folder)
        monkeypatch.setattr(
            ssr,
            "_build_infisical_handler",
            lambda config: _FakeInfisicalHandler(
                secrets, secret_path=config.secret_path
            ),
        )

        resolved = await resolve_api_key_async(minted_ref)

        assert resolved is not None
        assert resolved.get_secret_value() == _SUBMITTED_VALUE
