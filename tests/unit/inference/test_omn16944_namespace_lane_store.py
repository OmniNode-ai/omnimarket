# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16944 AC4: ``namespaces`` must be load-bearing in the lane store.

``ModelSecretNamespaceRule`` (omnibase_infra#3085) declares the *rule* a
runtime-MINTED BYOK ref resolves through, and ``SecretResolver`` already
consults it -- ``_get_source_spec`` returns the namespace-derived
``ModelSecretSourceSpec`` before convention fallback.

The half that was missing lives here. ``_lane_infisical_handler`` decided
whether this lane needs an Infisical handler *from* ``config.mappings`` alone,
so a lane whose only Infisical source is a namespace rule got
``infisical_handler=None``. ``SecretResolver`` then logged
"Infisical handler not configured" and returned ``None`` for every minted ref
-- and because the tenant-overlay wrapper rewrites a miss into "the tenant must
register this ref", a pure LANE misconfiguration was reported to the customer
as their own missing credential. That is the OMN-16891 failure class again: the
lane config reads as correctly configured while resolving nothing.

These tests pin four things:

* a lane whose ONLY Infisical source is a namespace rule builds the handler and
  resolves a minted ref from the folder the rule declares;
* the no-value case still RAISES -- declaring a namespace widens which refs have
  a source, never what happens when the source holds nothing, and never
  borrows a house key;
* a namespace folder is never promoted to the handler's default
  ``secret_path``: that default is what a folder-LESS house mapping inherits,
  and a tenant-partitioned folder must never become it;
* an Infisical namespace whose template declares no folder is a loud refusal at
  store construction -- such a rule would make every tenant's minted ref
  inherit the handler default, i.e. read from wherever the platform's own keys
  live (the OMN-15631 drift).

No secret VALUE appears in any assertion message.
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
    SecretStoreConfigurationError,
    clear_secret_store_resolver_cache,
    resolve_api_key_async,
)

pytestmark = pytest.mark.unit

_INFISICAL_BOOTSTRAP_VARS = (
    "INFISICAL_ADDR",
    "INFISICAL_CLIENT_ID",
    "INFISICAL_CLIENT_SECRET",
    "INFISICAL_PROJECT_ID",
    "INFISICAL_ENVIRONMENT_SLUG",
)

_PROJECT_UUID = "e5010c63-94b3-43ed-9554-0d2dcf3c4e36"

# The tenant-credential folder the WRITE half roots its store at
# (``credential_publisher._DEFAULT_TENANT_CREDENTIAL_SECRET_PATH``). Spelled
# here as a literal on purpose: the two halves agreeing is the property under
# test, and importing the constant would make the assertion vacuous.
_TENANT_FOLDER = "/tenant-inference-credentials"
_HOUSE_FOLDER = "/dev/onex-runtime"

# A ref of the exact shape ``credential_publisher.mint_api_key_ref`` produces:
# ``cred_{tenant_id}_{provider}_{uuid4().hex}``.
_MINTED_REF = "cred_af432b87_openrouter_0f1e2d3c4b5a69788796a5b4c3d2e1f0"

# The namespace rule the onex-dev lane declares, byte-for-byte
# (omninode_infra#1198, k8s/onex-dev/runtime/configmap.yaml).
_NAMESPACE_YAML = textwrap.dedent(f"""\
    namespaces:
      - namespace: tenant_inference_credentials
        ref_pattern: '^cred_[A-Za-z0-9._:-]+_[A-Za-z0-9_-]+_[0-9a-f]{{32}}$'
        source_type: infisical
        source_path_template: {_TENANT_FOLDER}/{{ref}}
""")

# A lane whose ONLY Infisical source is the namespace rule. Its mappings are
# env-sourced bootstrap, exactly like the gateway/keycloak refs on the real
# lane -- nothing here names an Infisical mapping.
_NAMESPACE_ONLY_LANE_YAML = (
    textwrap.dedent("""\
    enable_convention_fallback: false
    mappings:
      - logical_name: gateway.attach.keycloak.issuer
        source:
          source_type: env
          source_path: KEYCLOAK_ISSUER
    """)
    + _NAMESPACE_YAML
)

# The deployed onex-dev shape: one folder-qualified house mapping PLUS the
# namespace rule.
_BYOK_LANE_YAML = (
    textwrap.dedent(f"""\
    enable_convention_fallback: false
    mappings:
      - logical_name: database.tenant_projection.dsn
        source:
          source_type: infisical
          source_path: {_HOUSE_FOLDER}/ONEX_TENANT_DB_URL
    """)
    + _NAMESPACE_YAML
)


class _FakeInfisicalHandler:
    """Stands in for ``HandlerInfisical``; addresses secrets by (folder, name)."""

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
    for name in (*_INFISICAL_BOOTSTRAP_VARS, "INFISICAL_REQUIRED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_JSON", raising=False)
    clear_secret_store_resolver_cache()
    yield
    clear_secret_store_resolver_cache()


def _set_bootstrap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFISICAL_ADDR", "http://infisical.example.invalid:8080")
    monkeypatch.setenv("INFISICAL_CLIENT_ID", "id-not-a-secret-value")
    monkeypatch.setenv("INFISICAL_CLIENT_SECRET", "unit-test-placeholder")
    monkeypatch.setenv("INFISICAL_PROJECT_ID", _PROJECT_UUID)
    monkeypatch.setenv("INFISICAL_ENVIRONMENT_SLUG", "dev")


def _render_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, yaml_text: str
) -> None:
    config_file = tmp_path / "secret_resolver.yaml"
    config_file.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", str(config_file))
    clear_secret_store_resolver_cache()


def _install_fake_handler(
    monkeypatch: pytest.MonkeyPatch, secrets: dict[str, str]
) -> dict[str, Any]:
    """Replace only the CONSTRUCTION seam; the wiring under test stays real."""
    built: dict[str, Any] = {}

    def _fake_build(config: Any) -> _FakeInfisicalHandler:
        handler = _FakeInfisicalHandler(secrets, secret_path=config.secret_path)
        built["config"] = config
        built["handler"] = handler
        return handler

    monkeypatch.setattr(ssr, "_build_infisical_handler", _fake_build)
    return built


class TestNamespaceOnlyLaneResolves:
    """A lane whose only Infisical source is a namespace rule must still read."""

    async def test_minted_ref_resolves_from_the_namespace_declared_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_bootstrap_env(monkeypatch)
        _render_lane(tmp_path, monkeypatch, _NAMESPACE_ONLY_LANE_YAML)
        built = _install_fake_handler(
            monkeypatch, {f"{_TENANT_FOLDER}/{_MINTED_REF}": "tenant-registered-value"}
        )

        resolved = await resolve_api_key_async(_MINTED_REF)

        assert isinstance(resolved, SecretStr)
        assert resolved.get_secret_value() == "tenant-registered-value"
        # The folder travelled with the read; it was not inherited from a
        # handler default that no mapping declares on this lane.
        assert built["handler"].reads == [(_MINTED_REF, _TENANT_FOLDER)]

    async def test_namespace_rule_alone_is_enough_to_build_the_handler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The construction seam is reached at all -- the defect, stated directly."""
        _set_bootstrap_env(monkeypatch)
        _render_lane(tmp_path, monkeypatch, _NAMESPACE_ONLY_LANE_YAML)
        built = _install_fake_handler(monkeypatch, {})

        with pytest.raises(SecretResolutionError):
            await resolve_api_key_async(_MINTED_REF)

        assert "handler" in built, (
            "a lane declaring an Infisical namespace rule built no Infisical "
            "handler, so every minted ref resolves to None behind a WARNING"
        )

    async def test_declared_namespace_without_credentials_names_the_namespace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2 shape: an unbuildable store is a lane fact, attributably reported."""
        _render_lane(tmp_path, monkeypatch, _NAMESPACE_ONLY_LANE_YAML)  # no INFISICAL_*

        with pytest.raises(SecretStoreConfigurationError) as excinfo:
            await resolve_api_key_async(_MINTED_REF)

        message = str(excinfo.value)
        assert "tenant_inference_credentials" in message
        assert "INFISICAL_CLIENT_ID" in message
        assert "INFISICAL_PROJECT_ID" in message


class TestNamespaceStillFailsClosed:
    """AC3 -- declaring a namespace never creates a value, and never a fallback."""

    async def test_no_stored_value_raises_and_never_borrows_a_house_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_bootstrap_env(monkeypatch)
        _render_lane(tmp_path, monkeypatch, _NAMESPACE_ONLY_LANE_YAML)
        _install_fake_handler(monkeypatch, {})  # store holds nothing
        # House keys planted on every surface a fallback could reach.
        monkeypatch.setenv("OPENROUTER_API_KEY", "house-key-must-not-be-used")
        monkeypatch.setenv(_MINTED_REF, "house-key-must-not-be-used")

        with pytest.raises(SecretResolutionError) as excinfo:
            await resolve_api_key_async(
                _MINTED_REF, env_var_fallback="OPENROUTER_API_KEY"
            )

        message = str(excinfo.value)
        assert "house-key-must-not-be-used" not in message
        assert "af432b87" in message  # tenant-attributed, per OMN-15631

    async def test_no_stored_value_is_none_when_not_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_bootstrap_env(monkeypatch)
        _render_lane(tmp_path, monkeypatch, _NAMESPACE_ONLY_LANE_YAML)
        _install_fake_handler(monkeypatch, {})
        monkeypatch.setenv("OPENROUTER_API_KEY", "house-key-must-not-be-used")

        assert (
            await resolve_api_key_async(
                _MINTED_REF, required=False, env_var_fallback="OPENROUTER_API_KEY"
            )
            is None
        )


class TestNamespaceFolderIsNotTheHandlerDefault:
    """A tenant-partitioned folder must never become the house default."""

    async def test_deployed_lane_keeps_the_mapping_folder_as_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real onex-dev shape: one house mapping folder plus the namespace.

        The handler's configured ``secret_path`` is what a folder-LESS source
        inherits. It is derived from the lane's MAPPINGS; adding a namespace
        rule must not move it onto the tenant folder.
        """
        _set_bootstrap_env(monkeypatch)
        _render_lane(tmp_path, monkeypatch, _BYOK_LANE_YAML)
        built = _install_fake_handler(
            monkeypatch,
            {
                f"{_HOUSE_FOLDER}/ONEX_TENANT_DB_URL": "house-dsn",
                f"{_TENANT_FOLDER}/{_MINTED_REF}": "tenant-registered-value",
            },
        )

        house = await resolve_api_key_async("database.tenant_projection.dsn")
        tenant = await resolve_api_key_async(_MINTED_REF)

        assert house is not None
        assert house.get_secret_value() == "house-dsn"
        assert tenant is not None
        assert tenant.get_secret_value() == "tenant-registered-value"
        assert built["config"].secret_path != _TENANT_FOLDER
        assert built["config"].secret_path == _HOUSE_FOLDER
        assert built["handler"].reads == [
            ("ONEX_TENANT_DB_URL", _HOUSE_FOLDER),
            (_MINTED_REF, _TENANT_FOLDER),
        ]

    async def test_folderless_namespace_template_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``source_path_template: '{ref}'`` would read the house folder.

        A folder-less template declares no folder of its own, so every matched
        tenant ref inherits the handler's configured default -- the folder the
        platform's own keys live in. That is exactly the tenant/house crossing
        OMN-15631 forbids, so it is a refusal at store construction naming the
        namespace, never a silent read.
        """
        _set_bootstrap_env(monkeypatch)
        _render_lane(
            tmp_path,
            monkeypatch,
            textwrap.dedent(f"""\
                enable_convention_fallback: false
                mappings:
                  - logical_name: database.tenant_projection.dsn
                    source:
                      source_type: infisical
                      source_path: {_HOUSE_FOLDER}/ONEX_TENANT_DB_URL
                namespaces:
                  - namespace: tenant_inference_credentials
                    ref_pattern: '^cred_[A-Za-z0-9._:-]+_[A-Za-z0-9_-]+_[0-9a-f]{{32}}$'
                    source_type: infisical
                    source_path_template: '{{ref}}'
            """),
        )
        _install_fake_handler(monkeypatch, {f"{_HOUSE_FOLDER}/{_MINTED_REF}": "house"})

        with pytest.raises(SecretStoreConfigurationError) as excinfo:
            await resolve_api_key_async(_MINTED_REF)

        message = str(excinfo.value)
        assert "tenant_inference_credentials" in message
        assert "house" not in message


class TestUnrelatedLanesAreUnaffected:
    """The gate stays scoped -- no lane starts demanding Infisical credentials."""

    async def test_file_namespace_alone_does_not_demand_infisical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate keys on the namespace's SOURCE TYPE, not on its existence.

        A ``file``-sourced namespace is store-backed too, but it needs no
        Infisical machine identity. With no ``INFISICAL_*`` variable set at all
        this lane must still CONSTRUCT its store -- the miss it then reports is
        an absent VALUE (``SecretResolutionError``), not an unbuildable store
        (``SecretStoreConfigurationError``). The sibling
        ``test_declared_namespace_without_credentials_names_the_namespace`` is
        the positive control: the same absent bootstrap DOES refuse when the
        namespace is Infisical-sourced.
        """
        _render_lane(
            tmp_path,
            monkeypatch,
            textwrap.dedent("""\
                enable_convention_fallback: false
                mappings: []
                namespaces:
                  - namespace: tenant_inference_credentials
                    ref_pattern: '^cred_[A-Za-z0-9._:-]+_[A-Za-z0-9_-]+_[0-9a-f]{32}$'
                    source_type: file
                    source_path_template: '{ref}'
            """),
        )

        with pytest.raises(SecretResolutionError):
            await resolve_api_key_async(_MINTED_REF)

    async def test_lane_with_no_namespaces_is_byte_identical_in_behaviour(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _render_lane(
            tmp_path,
            monkeypatch,
            textwrap.dedent("""\
                enable_convention_fallback: false
                mappings:
                  - logical_name: gateway.attach.keycloak.issuer
                    source:
                      source_type: env
                      source_path: KEYCLOAK_ISSUER
            """),
        )
        monkeypatch.setenv("KEYCLOAK_ISSUER", "https://issuer.example.invalid/realms/x")

        resolved = await resolve_api_key_async("gateway.attach.keycloak.issuer")

        assert resolved is not None
