# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16984 / OMN-16944: the lane-mapped store must be able to READ Infisical.

Before this ticket ``_configured_secret_store()`` built
``SecretResolver(config=config)`` with no ``infisical_handler``. Every
``source_type: infisical`` mapping therefore resolved to ``None`` behind a
WARNING -- the lane config read as correctly configured while resolving
nothing (the OMN-16891 failure class). Two further defects rode along:

* the rendered config file was read with a bare ``Path.read_text()``, so a
  workload that inherits ``ONEX_SECRET_RESOLVER_CONFIG_PATH`` from a
  namespace-wide ConfigMap but does not ship the rendered artifact crashed
  with ``FileNotFoundError`` instead of a typed, attributable error;
* nothing forced a lane that DECLARES an Infisical source to actually be able
  to reach it.

These tests pin all three. No secret VALUE appears in any assertion message.
"""

from __future__ import annotations

import json
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

_INFISICAL_BOOTSTRAP_VARS = (
    "INFISICAL_ADDR",
    "INFISICAL_CLIENT_ID",
    "INFISICAL_CLIENT_SECRET",
    "INFISICAL_PROJECT_ID",
    "INFISICAL_ENVIRONMENT_SLUG",
)

_PROJECT_UUID = "e5010c63-94b3-43ed-9554-0d2dcf3c4e36"

# The staging lane's real shape after OMN-16984: the provider keys resolve from
# the Infisical folder the least-privilege store identity can read, and the
# gateway/keycloak refs stay env-sourced bootstrap.
_LANE_CONFIG_YAML = textwrap.dedent("""\
    enable_convention_fallback: false
    mappings:
      - logical_name: llm.glm.api_key
        source:
          source_type: infisical
          source_path: /dev/onex-runtime/LLM_GLM_API_KEY
      - logical_name: gateway.attach.keycloak.issuer
        source:
          source_type: env
          source_path: KEYCLOAK_ISSUER
""")


class _FakeInfisicalHandler:
    """Stands in for ``HandlerInfisical`` -- sync surface SecretResolver uses."""

    def __init__(self, secrets: dict[str, str], *, secret_path: str) -> None:
        self._secrets = secrets
        self.secret_path = secret_path
        self.requested: list[str] = []

    def get_secret_sync(
        self,
        *,
        secret_name: str,
        project_id: str | None = None,
        environment_slug: str | None = None,
        secret_path: str | None = None,
    ) -> SecretStr | None:
        self.requested.append(secret_name)
        value = self._secrets.get(secret_name)
        return SecretStr(value) if value is not None else None

    async def execute(self, envelope: dict[str, Any]) -> Any:
        """Async surface ``SecretResolver._read_infisical_secret_async`` drives."""
        payload = envelope["payload"]
        secret_name = payload["secret_name"]
        self.requested.append(secret_name)
        value = self._secrets.get(secret_name)
        return SimpleNamespace(result={"value": value} if value is not None else {})


@pytest.fixture(autouse=True)
def _isolated_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*_INFISICAL_BOOTSTRAP_VARS, "INFISICAL_REQUIRED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_JSON", raising=False)
    clear_secret_store_resolver_cache()
    yield
    clear_secret_store_resolver_cache()


def _set_bootstrap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFISICAL_ADDR", "http://infisical.dev.svc.cluster.local:8080")
    monkeypatch.setenv("INFISICAL_CLIENT_ID", "id-not-a-secret-value")
    monkeypatch.setenv("INFISICAL_CLIENT_SECRET", "unit-test-placeholder")
    monkeypatch.setenv("INFISICAL_PROJECT_ID", _PROJECT_UUID)
    monkeypatch.setenv("INFISICAL_ENVIRONMENT_SLUG", "dev")


def _render_lane_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_file = tmp_path / "secret_resolver.yaml"
    config_file.write_text(_LANE_CONFIG_YAML, encoding="utf-8")
    monkeypatch.setenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", str(config_file))
    clear_secret_store_resolver_cache()
    return config_file


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


class TestInfisicalHandlerIsWired:
    """AC1/AC4 -- a declared Infisical source actually reads Infisical."""

    async def test_infisical_mapping_resolves_through_the_wired_handler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_bootstrap_env(monkeypatch)
        _render_lane_config(tmp_path, monkeypatch)
        built = _install_fake_handler(
            monkeypatch, {"LLM_GLM_API_KEY": "value-from-infisical"}
        )

        resolved = await resolve_api_key_async("llm.glm.api_key")

        assert isinstance(resolved, SecretStr)
        assert resolved.get_secret_value() == "value-from-infisical"
        # Fails loudly if the handler argument is ever dropped again.
        assert built["handler"].requested == ["LLM_GLM_API_KEY"]

    async def test_handler_secret_path_comes_from_the_declared_mapping_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mapping is the authority for the folder -- not a new env var."""
        _set_bootstrap_env(monkeypatch)
        _render_lane_config(tmp_path, monkeypatch)
        built = _install_fake_handler(
            monkeypatch, {"LLM_GLM_API_KEY": "value-from-infisical"}
        )

        await resolve_api_key_async("llm.glm.api_key")

        assert built["config"].secret_path == "/dev/onex-runtime"
        assert built["config"].environment_slug == "dev"

    async def test_env_mapping_on_the_same_lane_is_unaffected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_bootstrap_env(monkeypatch)
        monkeypatch.setenv("KEYCLOAK_ISSUER", "https://issuer.example.invalid/realms/x")
        _render_lane_config(tmp_path, monkeypatch)
        _install_fake_handler(monkeypatch, {})

        resolved = await resolve_api_key_async("gateway.attach.keycloak.issuer")

        assert isinstance(resolved, SecretStr)
        assert resolved.get_secret_value() == "https://issuer.example.invalid/realms/x"


class TestDeclaredButUnreadableIsLoud:
    """AC2 -- fail fast at store construction, naming names, never values."""

    async def test_declared_infisical_source_without_credentials_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _render_lane_config(tmp_path, monkeypatch)  # no INFISICAL_* env at all

        with pytest.raises(SecretStoreConfigurationError) as excinfo:
            await resolve_api_key_async("llm.glm.api_key")

        message = str(excinfo.value)
        assert "llm.glm.api_key" in message
        assert "INFISICAL_CLIENT_ID" in message
        assert "INFISICAL_CLIENT_SECRET" in message

    async def test_error_names_variables_never_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("INFISICAL_ADDR", "http://infisical.invalid:8080")
        monkeypatch.setenv("INFISICAL_CLIENT_ID", "id-not-a-secret-value")
        monkeypatch.setenv("INFISICAL_CLIENT_SECRET", "unit-test-placeholder")
        # INFISICAL_PROJECT_ID / _ENVIRONMENT_SLUG deliberately absent.
        _render_lane_config(tmp_path, monkeypatch)

        with pytest.raises(SecretStoreConfigurationError) as excinfo:
            await resolve_api_key_async("llm.glm.api_key")

        message = str(excinfo.value)
        assert "INFISICAL_PROJECT_ID" in message
        assert "INFISICAL_ENVIRONMENT_SLUG" in message
        assert "unit-test-placeholder" not in message
        assert "id-not-a-secret-value" not in message

    async def test_infisical_required_lane_without_credentials_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INFISICAL_REQUIRED=true declares a controlled lane: the store must be
        constructible even when no mapping happens to name an Infisical source."""
        config_file = tmp_path / "secret_resolver.yaml"
        config_file.write_text(
            textwrap.dedent("""\
                enable_convention_fallback: false
                mappings:
                  - logical_name: gateway.attach.keycloak.issuer
                    source:
                      source_type: env
                      source_path: KEYCLOAK_ISSUER
            """),
            encoding="utf-8",
        )
        monkeypatch.setenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", str(config_file))
        monkeypatch.setenv("INFISICAL_REQUIRED", "true")
        clear_secret_store_resolver_cache()

        with pytest.raises(SecretStoreConfigurationError, match="INFISICAL_REQUIRED"):
            await resolve_api_key_async("gateway.attach.keycloak.issuer")

    async def test_uncontrolled_lane_without_infisical_sources_needs_no_handler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate is scoped: a lane that declares neither an Infisical source
        nor INFISICAL_REQUIRED must not start demanding Infisical credentials."""
        config_file = tmp_path / "secret_resolver.yaml"
        config_file.write_text(
            textwrap.dedent("""\
                enable_convention_fallback: false
                mappings:
                  - logical_name: gateway.attach.keycloak.issuer
                    source:
                      source_type: env
                      source_path: KEYCLOAK_ISSUER
            """),
            encoding="utf-8",
        )
        monkeypatch.setenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", str(config_file))
        monkeypatch.setenv("KEYCLOAK_ISSUER", "https://issuer.example.invalid/realms/x")
        clear_secret_store_resolver_cache()

        resolved = await resolve_api_key_async("gateway.attach.keycloak.issuer")

        assert isinstance(resolved, SecretStr)

    async def test_two_infisical_folders_on_one_lane_are_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SecretResolver drops the folder from source_path and reads through the
        handler's configured path, so exactly one folder is expressible today.
        Two must be a loud refusal, never a silent read from the wrong folder."""
        _set_bootstrap_env(monkeypatch)
        config_file = tmp_path / "secret_resolver.yaml"
        config_file.write_text(
            textwrap.dedent("""\
                enable_convention_fallback: false
                mappings:
                  - logical_name: llm.glm.api_key
                    source:
                      source_type: infisical
                      source_path: /dev/onex-runtime/LLM_GLM_API_KEY
                  - logical_name: llm.tenant.api_key
                    source:
                      source_type: infisical
                      source_path: /tenant-inference-credentials/CRED_X
            """),
            encoding="utf-8",
        )
        monkeypatch.setenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", str(config_file))
        clear_secret_store_resolver_cache()

        with pytest.raises(SecretStoreConfigurationError) as excinfo:
            await resolve_api_key_async("llm.glm.api_key")

        message = str(excinfo.value)
        assert "/dev/onex-runtime" in message
        assert "/tenant-inference-credentials" in message


class TestStillFailsClosed:
    """AC3 -- a genuine miss never degrades into a house key."""

    async def test_absent_infisical_value_still_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_bootstrap_env(monkeypatch)
        _render_lane_config(tmp_path, monkeypatch)
        _install_fake_handler(monkeypatch, {})  # store holds nothing
        # A same-named house key is present in the ambient environment.
        monkeypatch.setenv("LLM_GLM_API_KEY", "house-key-must-not-be-used")

        with pytest.raises(SecretResolutionError) as excinfo:
            await resolve_api_key_async("llm.glm.api_key")

        assert "house-key-must-not-be-used" not in str(excinfo.value)

    async def test_absent_infisical_value_is_none_when_not_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_bootstrap_env(monkeypatch)
        _render_lane_config(tmp_path, monkeypatch)
        _install_fake_handler(monkeypatch, {})
        monkeypatch.setenv("LLM_GLM_API_KEY", "house-key-must-not-be-used")

        assert await resolve_api_key_async("llm.glm.api_key", required=False) is None


class TestRenderedConfigIsOptionalNotSilent:
    """The namespace-wide ConfigMap injects ONEX_SECRET_RESOLVER_CONFIG_PATH into
    every pod, but only the runtime image's entrypoint renders the artifact it
    names. onex-api and omnimarket-projection-* therefore crashed with a bare
    FileNotFoundError at store construction. The declared inline JSON -- the SAME
    source the renderer itself reads first -- is the authority when the rendered
    artifact is absent; neither present is a typed refusal, not a fallback."""

    async def test_absent_rendered_file_resolves_from_declared_inline_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "ONEX_SECRET_RESOLVER_CONFIG_PATH", str(tmp_path / "never-rendered.yaml")
        )
        monkeypatch.setenv(
            "ONEX_SECRET_RESOLVER_CONFIG_JSON",
            json.dumps(
                {
                    "enable_convention_fallback": False,
                    "mappings": [
                        {
                            "logical_name": "gateway.attach.keycloak.issuer",
                            "source": {
                                "source_type": "env",
                                "source_path": "KEYCLOAK_ISSUER",
                            },
                        }
                    ],
                }
            ),
        )
        monkeypatch.setenv("KEYCLOAK_ISSUER", "https://issuer.example.invalid/realms/x")
        clear_secret_store_resolver_cache()

        resolved = await resolve_api_key_async("gateway.attach.keycloak.issuer")

        assert isinstance(resolved, SecretStr)
        assert resolved.get_secret_value() == "https://issuer.example.invalid/realms/x"

    async def test_absent_rendered_file_and_no_inline_json_raises_typed_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "never-rendered.yaml"
        monkeypatch.setenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", str(missing))
        clear_secret_store_resolver_cache()

        with pytest.raises(SecretStoreConfigurationError) as excinfo:
            await resolve_api_key_async("gateway.attach.keycloak.issuer")

        message = str(excinfo.value)
        assert str(missing) in message
        assert "ONEX_SECRET_RESOLVER_CONFIG_JSON" in message

    async def test_rendered_file_wins_over_inline_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rendered artifact is what the renderer validated and chmod'd; it
        stays authoritative wherever it exists."""
        config_file = tmp_path / "secret_resolver.yaml"
        config_file.write_text(
            textwrap.dedent("""\
                enable_convention_fallback: false
                mappings:
                  - logical_name: gateway.attach.keycloak.issuer
                    source:
                      source_type: env
                      source_path: RENDERED_ISSUER
            """),
            encoding="utf-8",
        )
        monkeypatch.setenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", str(config_file))
        monkeypatch.setenv(
            "ONEX_SECRET_RESOLVER_CONFIG_JSON",
            json.dumps(
                {
                    "mappings": [
                        {
                            "logical_name": "gateway.attach.keycloak.issuer",
                            "source": {
                                "source_type": "env",
                                "source_path": "INLINE_ISSUER",
                            },
                        }
                    ]
                }
            ),
        )
        monkeypatch.setenv("RENDERED_ISSUER", "from-rendered-file")
        monkeypatch.setenv("INLINE_ISSUER", "from-inline-json")
        clear_secret_store_resolver_cache()

        resolved = await resolve_api_key_async("gateway.attach.keycloak.issuer")

        assert resolved is not None
        assert resolved.get_secret_value() == "from-rendered-file"

    async def test_malformed_inline_json_raises_typed_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "ONEX_SECRET_RESOLVER_CONFIG_PATH", str(tmp_path / "never-rendered.yaml")
        )
        monkeypatch.setenv("ONEX_SECRET_RESOLVER_CONFIG_JSON", "{not json")
        clear_secret_store_resolver_cache()

        with pytest.raises(
            SecretStoreConfigurationError, match="ONEX_SECRET_RESOLVER_CONFIG_JSON"
        ):
            await resolve_api_key_async("gateway.attach.keycloak.issuer")
