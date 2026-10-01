# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Provider quota reads bind through the owning contract and lane topology."""

import asyncio
from datetime import UTC, datetime
from importlib.resources import files
from threading import get_ident
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.core.model_deployment_topology import ModelDeploymentTopology
from pydantic import SecretStr

from omnimarket.inference.provider_quota_state import (
    PostgresProviderQuotaReader,
    ProviderQuotaReadBindingError,
    provider_quota_read_declaration,
)
from omnimarket.inference.provider_quota_state import (
    resolve_provider_quota_reader as _real_resolve,
)

pytestmark = pytest.mark.unit

_TENANT_REF = "database.tenant_projection.dsn"
_RUNTIME_REF = "database.omninode_runtime.dsn"


class FakeSecretResolver:
    def __init__(self, secrets: dict[str, SecretStr]) -> None:
        self.secrets = secrets
        self.asked: list[str] = []

    def get_secret(self, name: str, required: bool = False) -> SecretStr | None:
        self.asked.append(name)
        return self.secrets.get(name)


@pytest.fixture
def topology() -> ModelDeploymentTopology:
    source = files("omnibase_infra.topology") / "instances" / "local.yaml"
    raw = yaml.safe_load(source.read_text())
    application = raw["databases"]["application"]
    for binding_ref, secret_ref in (
        ("tenant_projection", _TENANT_REF),
        ("omninode_runtime_service", _RUNTIME_REF),
    ):
        binding = application["bindings"][binding_ref]
        binding.pop("dsn_env", None)
        binding["secret_ref"] = secret_ref
    grants = application["principals"]["tenant_projection_writer"]["grants"]
    for grant in grants:
        if grant["object_type"] == "TABLE" and grant.get("schema") == "public":
            if "provider_quota_state" not in grant["objects"]:
                grant["objects"].append("provider_quota_state")
            grant["privileges"] = sorted(
                set(grant["privileges"]) | {"SELECT", "INSERT", "UPDATE"}
            )
            break
    else:
        grants.append(
            {
                "object_type": "TABLE",
                "schema": "public",
                "objects": ["provider_quota_state"],
                "privileges": ["SELECT", "INSERT", "UPDATE"],
            }
        )
    return ModelDeploymentTopology.model_validate(raw)


@pytest.fixture
def secret_resolver() -> FakeSecretResolver:
    # Opaque test carriers: resolution never opens a database connection.
    return FakeSecretResolver(
        {
            _TENANT_REF: SecretStr("test-tenant-carrier"),
            _RUNTIME_REF: SecretStr("test-runtime-carrier"),
        }
    )


def test_the_reader_resolves_from_the_overlay(
    topology: ModelDeploymentTopology, secret_resolver: FakeSecretResolver
) -> None:
    reader = _real_resolve(topology=topology, secret_resolver=secret_resolver)
    assert isinstance(reader, PostgresProviderQuotaReader)
    assert reader.binding_ref == "tenant_projection"
    assert reader.expected_principal == "tenant_projection_writer"
    assert (
        reader._dsn.get_secret_value()
        == secret_resolver.secrets[_TENANT_REF].get_secret_value()
    )
    assert _TENANT_REF in secret_resolver.asked
    assert reader._conn is None


def test_no_env_var_is_consulted(
    monkeypatch: pytest.MonkeyPatch,
    topology: ModelDeploymentTopology,
    secret_resolver: FakeSecretResolver,
) -> None:
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "garbage-not-a-dsn")
    monkeypatch.setenv("ONEX_TENANT_DB_URL", "garbage-not-a-dsn")
    reader = _real_resolve(topology=topology, secret_resolver=secret_resolver)
    assert (
        reader._dsn.get_secret_value()
        == secret_resolver.secrets[_TENANT_REF].get_secret_value()
    )


def test_a_missing_overlay_entry_raises_a_typed_error(
    topology: ModelDeploymentTopology, secret_resolver: FakeSecretResolver
) -> None:
    del secret_resolver.secrets[_TENANT_REF]
    with pytest.raises(ProviderQuotaReadBindingError, match="tenant_projection") as exc:
        _real_resolve(topology=topology, secret_resolver=secret_resolver)
    assert all(
        value.get_secret_value() not in str(exc.value)
        for value in secret_resolver.secrets.values()
    )


def test_an_overlay_without_the_grant_raises_a_typed_error(
    topology: ModelDeploymentTopology, secret_resolver: FakeSecretResolver
) -> None:
    raw = topology.model_dump(mode="json", exclude_none=True)
    grants = raw["databases"]["application"]["principals"]["tenant_projection_writer"][
        "grants"
    ]
    for grant in grants:
        if "objects" in grant:
            grant["objects"] = [
                name for name in grant["objects"] if name != "provider_quota_state"
            ]
    without_grant = ModelDeploymentTopology.model_validate(raw)
    with pytest.raises(ProviderQuotaReadBindingError, match="provider_quota_state"):
        _real_resolve(topology=without_grant, secret_resolver=secret_resolver)


def test_no_selected_overlay_raises_even_with_the_dsn_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_DATABASE_TOPOLOGY_PROFILE", raising=False)
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql://u@h/db")
    with pytest.raises(
        ProviderQuotaReadBindingError, match="no lane database-topology"
    ):
        _real_resolve()


def test_the_read_declaration_comes_from_the_owning_contract() -> None:
    declaration = provider_quota_read_declaration()
    assert declaration.name == "provider_quota_state"
    assert declaration.schema == "public"
    assert declaration.database_ref == "application"
    assert declaration.access == "read"


def test_the_quota_module_names_no_dsn_env_var() -> None:
    source = files("omnimarket.inference") / "provider_quota_state.py"
    assert "OMNIDASH_ANALYTICS_DB_URL" not in source.read_text()


@pytest.mark.parametrize("running_loop", [False, True])
def test_lane_bootstrap_builds_the_resolver_with_or_without_a_running_loop(
    monkeypatch: pytest.MonkeyPatch,
    topology: ModelDeploymentTopology,
    secret_resolver: FakeSecretResolver,
    running_loop: bool,
) -> None:
    monkeypatch.setenv("ONEX_DATABASE_TOPOLOGY_PROFILE", "test-quota-lane")
    caller_thread = get_ident()
    resolver_threads: list[int] = []

    def load_profile(profile: str) -> ModelDeploymentTopology:
        assert profile == "test-quota-lane"
        return topology

    async def build_resolver(container: object | None) -> FakeSecretResolver:
        assert container is None
        resolver_threads.append(get_ident())
        return secret_resolver

    monkeypatch.setattr("omnibase_infra.topology.load_topology_profile", load_profile)
    monkeypatch.setattr(
        "omnibase_infra.runtime.auto_wiring.handler_wiring.build_topology_secret_resolver",
        build_resolver,
    )

    async def resolve_in_loop() -> PostgresProviderQuotaReader:
        return _real_resolve()

    reader = asyncio.run(resolve_in_loop()) if running_loop else _real_resolve()
    assert reader.binding_ref == "tenant_projection"
    assert len(resolver_threads) == 1
    assert (resolver_threads[0] != caller_thread) == running_loop
    assert {_TENANT_REF, _RUNTIME_REF} <= set(secret_resolver.asked)


def test_a_topology_load_failure_is_a_chained_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_DATABASE_TOPOLOGY_PROFILE", "test-quota-lane")
    failure = ValueError("invalid test topology")

    def load_profile(profile: str) -> ModelDeploymentTopology:
        raise failure

    monkeypatch.setattr("omnibase_infra.topology.load_topology_profile", load_profile)
    with pytest.raises(ProviderQuotaReadBindingError, match="could not load") as exc:
        _real_resolve()
    assert exc.value.__cause__ is failure


def test_the_shared_resolver_also_requires_the_watermark_binding(
    topology: ModelDeploymentTopology, secret_resolver: FakeSecretResolver
) -> None:
    del secret_resolver.secrets[_RUNTIME_REF]
    with pytest.raises(ProviderQuotaReadBindingError, match="omninode_runtime_service"):
        _real_resolve(topology=topology, secret_resolver=secret_resolver)


def test_a_principal_mismatch_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCursor:
        def __enter__(self) -> "FakeCursor":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def execute(self, sql: str) -> None:
            assert sql == "SELECT current_user"

        def fetchone(self) -> tuple[str]:
            return ("role_omnidash",)

    class FakeConnection:
        closed = False

        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def close(self) -> None:
            self.closed = True

    connection = FakeConnection()
    monkeypatch.setattr(
        "omnimarket.projection.postgres_read_database.connect_read_only",
        lambda *_args, **_kwargs: connection,
    )
    reader = PostgresProviderQuotaReader(
        "postgresql://x@y/z", expected_principal="tenant_projection_writer"
    )
    with pytest.raises(
        ProviderQuotaReadBindingError,
        match=r"tenant_projection_writer.*role_omnidash",
    ):
        reader.read_active_blocks(tenant_id=uuid4(), as_of=datetime.now(UTC))
    assert connection.closed
    assert reader._conn is None
