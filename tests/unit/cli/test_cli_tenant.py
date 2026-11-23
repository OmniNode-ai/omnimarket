# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex tenant`` creates a tenant, shows it, and registers and revokes its gateway key over the bus (OMN-26475).

The bus and the secret store are in memory. A spy stands in for omnibase_core's public ``publish_and_poll`` and
records every topic pair, so each command is shown to publish on its own operation's ``-requested`` topic and to
poll its own ``-completed`` topic. Those pairs are read here from the owning node's contract file, never typed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from click.testing import CliRunner, Result
from omnibase_core.enums.enum_event_bus_type import EnumEventBusType
from omnibase_core.models.event_bus.model_node_broker_binding import (
    ModelNodeBrokerBinding,
)

from omnimarket.handlers import cli_tenant
from omnimarket.handlers.cli_tenant import tenant_group
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from omnimarket.nodes.node_secret_store_effect.models import (
    ModelSecretStoreOverlay,
)

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
_PROVISION_CONTRACT = _NODES / "node_tenant_provision_effect/contract.yaml"
_API_KEY_CONTRACT = _NODES / "node_tenant_api_key_effect/contract.yaml"

_PREFIX = "onxk_"
_TENANT_ID = UUID("11111111-2222-3333-4444-555555555555")
_SLUG = "acme-test"
_KEY_ID = UUID("99999999-8888-7777-6666-555555555555")
_PLAINTEXT = re.compile(r"onxk_[A-Za-z0-9_-]{20,}")


def _provision_pair(operation: str) -> tuple[str, str]:
    topics = yaml.safe_load(_PROVISION_CONTRACT.read_text(encoding="utf-8"))["topics"]
    return topics[operation]["requested"], topics[operation]["completed"]


def _api_key_pair(operation: str, result_event_type: str) -> tuple[str, str]:
    raw = yaml.safe_load(_API_KEY_CONTRACT.read_text(encoding="utf-8"))
    requested = next(
        handler["topic"]
        for handler in raw["handler_routing"]["handlers"]
        if handler["operation"] == operation
    )
    completed = next(
        event["topic"]
        for event in raw["published_events"]
        if event["event_type"] == result_event_type
    )
    return requested, completed


_CREATE = _provision_pair("tenant_provision")
_READ = _provision_pair("tenant_read")
_REGISTER = _api_key_pair(
    "tenant_api_key_register_requested", "ModelTenantApiKeyRegisterResult"
)
_REVOKE = _api_key_pair(
    "tenant_api_key_revoke_requested", "ModelTenantApiKeyRevokeResult"
)


@dataclass
class Call:
    node_id: str
    payload: dict[str, object]
    timeout: int
    bootstrap_servers: str
    command_topic: str
    response_topic: str


@dataclass
class Bus:
    """Answers by ``-requested`` topic. A ``None`` in a script is a poll that timed out."""

    script: dict[str, list[dict[str, object] | None]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def __call__(
        self,
        node_id: str,
        payload: dict[str, object],
        timeout: int,
        bootstrap_servers: str,
        command_topic: str,
        response_topic: str,
        inject_payload_correlation_id: bool = False,
        client_security: dict[str, str] | None = None,
    ) -> dict[str, object] | None:
        self.calls.append(
            Call(
                node_id,
                dict(payload),
                timeout,
                bootstrap_servers,
                command_topic,
                response_topic,
            )
        )
        queue = self.script.get(command_topic)
        assert queue, f"no scripted answer for {command_topic}"
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if answer is None:
            return None
        return {"correlation_id": str(uuid4()), "payload": answer}

    def on(self, topics: tuple[str, str]) -> list[Call]:
        return [c for c in self.calls if c.command_topic == topics[0]]


class _Store:
    def __init__(self, *, existing: bool = False) -> None:
        self.secrets: dict[str, str] = {}
        self._existing = existing

    async def get_secret(self, key: str) -> str | None:
        return self.secrets.get(key)

    async def set_secret(self, key: str, value: str) -> bool:
        if self._existing or key in self.secrets:
            return False
        self.secrets[key] = value
        return True

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("not offered")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        return sorted(self.secrets)

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        return None


def _store_overlay(environment: str | None) -> ModelSecretStoreOverlay:
    return ModelSecretStoreOverlay(
        provider="fake",
        address="http://127.0.0.1:9",
        project_id="00000000-0000-0000-0000-000000000001",
        environment=environment,
        client_id_ref="id-ref",
        client_secret_ref="secret-ref",
    )


def _write_overlays(
    root: Path, *, api_key_lane: str | None = "dev", store_env: str | None = "dev"
) -> None:
    api_dir = root / "node_tenant_api_key_effect"
    api_dir.mkdir(parents=True)
    api_doc: dict[str, object] = {}
    if api_key_lane is not None:
        api_doc["lane"] = api_key_lane
    if store_env is not None:
        api_doc["dev_secret_store_environment"] = store_env
    (api_dir / "overlay.yaml").write_text(yaml.safe_dump(api_doc), encoding="utf-8")
    gateway_dir = root / "node_workflow_gateway_effect"
    gateway_dir.mkdir(parents=True)
    gateway_doc = {
        "identity": {
            "jwks_url": "https://idp.example.test/jwks",
            "issuer": "https://idp.example.test",
            "audience": "unit-test",
            "algorithms": ["RS256"],
            "tenant_id_claim": "tenant_id",
            "tenant_slug_claim": "tenant_slug",
            "principal_id_claim": "sub",
            "jwks_cache_seconds": 300,
            "leeway_seconds": 30,
        },
        "database": {
            "secret_resolver_config_path": "unit-test",
            "dsn_secret": "unit.test.dsn",
            "pool_max_size": 2,
            "query_timeout_seconds": 5,
        },
        "api_key": {"header": "x-api-key", "prefix": _PREFIX},
        "rate_limit": {"refill_per_minute": 60, "burst_capacity": 10},
        "quota": {
            "metric_key": "m",
            "window_key": "w",
            "lock_namespace": "l",
            "rpm_metric_key": "rm",
            "rpm_window_key": "rw",
        },
        "catalog": {
            "workflows": [
                {
                    "workflow_type": "unit_test",
                    "contract_id": "unit-test",
                    "command_topic": "unit-test-topic",
                    "event_type": "UnitTest",
                    "enabled": True,
                    "payload_schema": {},
                    "wire": {
                        "payload_keys": ["prompt"],
                        "stamp_emitted_at": False,
                        "stamp_context_pack": False,
                        "canonical_tenant_id": False,
                        "stamp_tenant_principal": False,
                    },
                }
            ]
        },
        "gateway_instance_id": "unit-test",
        "envelope_schema_version": "1.0.0",
        "publish_timeout_seconds": 5,
    }
    (gateway_dir / "overlay.yaml").write_text(
        yaml.safe_dump(gateway_doc), encoding="utf-8"
    )


@dataclass
class World:
    bus: Bus
    store: _Store
    root: Path
    store_calls: list[str] = field(default_factory=list)

    def run(self, args: list[str], *, input: str | None = None) -> Result:
        return CliRunner().invoke(tenant_group, args, input=input)


def _tenant_found(state: str = "active") -> dict[str, object]:
    return {
        "command_id": str(uuid4()),
        "outcome": "found",
        "detail": "found",
        "tenant_id": str(_TENANT_ID),
        "tenant_slug": _SLUG,
        "name": "Acme Test",
        "provisioning_state": state,
        "plan_code": "free",
        "created_at": "2026-11-22T00:00:00Z",
        "emitted_at": "2026-11-22T00:00:01Z",
    }


def _registered(outcome: str = "registered") -> dict[str, object]:
    return {
        "correlation_id": str(uuid4()),
        "tenant_id": str(_TENANT_ID),
        "outcome": outcome,
        "credential_id": str(_KEY_ID),
        "created_at": "2026-11-22T00:00:02Z",
        "expires_at": "2026-11-25T00:00:02Z",
    }


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> World:
    root = tmp_path / "overlays"
    _write_overlays(root)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    monkeypatch.delenv("ONEX_LANE", raising=False)
    bus = Bus(
        script={
            _READ[0]: [_tenant_found()],
            _REGISTER[0]: [_registered()],
        }
    )
    store = _Store()
    monkeypatch.setattr(cli_tenant, "publish_and_poll", bus)
    monkeypatch.setattr(
        ModelNodeBrokerBinding,
        "for_node",
        classmethod(
            lambda cls, _node_id: cls(
                bus_type=EnumEventBusType.KAFKA,
                overlay_path=tmp_path / "overlay.yaml",
                lane="dev",
                bootstrap_servers="broker.invalid:9092",
            )
        ),
    )
    monkeypatch.setattr(
        cli_tenant, "_load_store_overlay", lambda: _store_overlay("dev")
    )
    monkeypatch.setattr(
        cli_tenant,
        "_make_secret_handler",
        lambda overlay: HandlerSecretStore(store=store, overlay=overlay),
    )
    monkeypatch.setattr(cli_tenant, "_stdout_is_tty", lambda: False)
    return World(bus=bus, store=store, root=root)


def _mint(*extra: str) -> list[str]:
    return ["key", "mint", "--tenant", _SLUG, "--name", "ci-key", *extra]


# --------------------------------------------------------------------------- create and show


def test_create_publishes_on_the_provision_topic_pair(world: World) -> None:
    command_id = str(uuid4())
    world.bus.script[_CREATE[0]] = [
        {
            "command_id": command_id,
            "outcome": "created",
            "detail": "created",
            "tenant_id": str(_TENANT_ID),
            "tenant_slug": _SLUG,
            "name": "Acme Test",
            "provisioning_state": "active",
            "plan_code": "free",
            "created_at": "2026-11-22T00:00:00Z",
            "emitted_at": "2026-11-22T00:00:01Z",
        }
    ]

    result = world.run(["create", "--slug", _SLUG, "--name", "Acme Test"])

    assert result.exit_code == 0, result.output
    (call,) = world.bus.calls
    assert (call.command_topic, call.response_topic) == _CREATE
    assert call.node_id == "node_tenant_provision_effect"
    assert call.bootstrap_servers == "broker.invalid:9092"
    assert set(call.payload) == {"command_id", "tenant_slug", "name"}
    assert call.payload["tenant_slug"] == _SLUG
    assert str(_TENANT_ID) in result.stdout


def test_create_refusal_is_printed_with_its_outcome_and_exits_one_without_resend(
    world: World,
) -> None:
    world.bus.script[_CREATE[0]] = [
        {
            "command_id": str(uuid4()),
            "outcome": "refused_invalid_slug",
            "detail": "slug is a reserved word",
            "emitted_at": "2026-11-22T00:00:01Z",
        }
    ]

    result = world.run(["create", "--slug", "admin", "--name", "Admin"])

    assert result.exit_code == 1
    assert "refused_invalid_slug" in result.output
    assert len(world.bus.calls) == 1


def test_show_by_slug_and_by_id_publish_on_the_read_topic_pair(world: World) -> None:
    by_slug = world.run(["show", "--slug", _SLUG])
    by_id = world.run(["show", "--id", str(_TENANT_ID)])

    assert by_slug.exit_code == 0, by_slug.output
    assert by_id.exit_code == 0, by_id.output
    first, second = world.bus.calls
    assert (first.command_topic, first.response_topic) == _READ
    assert (second.command_topic, second.response_topic) == _READ
    assert first.payload["tenant_slug"] == _SLUG
    assert "tenant_id" not in first.payload
    assert second.payload["tenant_id"] == str(_TENANT_ID)
    assert "tenant_slug" not in second.payload


def test_show_not_found_exits_one(world: World) -> None:
    world.bus.script[_READ[0]] = [
        {
            "command_id": str(uuid4()),
            "outcome": "not_found",
            "detail": "no such tenant",
            "emitted_at": "2026-11-22T00:00:01Z",
        }
    ]

    result = world.run(["show", "--slug", "nobody"])

    assert result.exit_code == 1
    assert "not_found" in result.output


@pytest.mark.parametrize(
    "args", [["show"], ["show", "--slug", _SLUG, "--id", str(_TENANT_ID)]]
)
def test_show_needs_exactly_one_of_id_and_slug_and_sends_nothing(
    world: World, args: list[str]
) -> None:
    result = world.run(args)

    assert result.exit_code != 0
    assert world.bus.calls == []


def test_a_poll_that_times_out_is_reported_and_exits_one(world: World) -> None:
    world.bus.script[_READ[0]] = [None]

    result = world.run(["show", "--slug", _SLUG])

    assert result.exit_code == 1
    assert "no answer" in result.output


def test_a_binding_that_needs_no_broker_is_refused_naming_the_overlay(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay_path = tmp_path / "inmemory-overlay.yaml"
    monkeypatch.setattr(
        ModelNodeBrokerBinding,
        "for_node",
        classmethod(
            lambda cls, _node_id: cls(
                bus_type=EnumEventBusType.INMEMORY, overlay_path=overlay_path
            )
        ),
    )

    result = world.run(["show", "--slug", _SLUG])

    assert result.exit_code == 1
    assert str(overlay_path) in result.output
    assert world.bus.calls == []


# --------------------------------------------------------------------------- key mint


def test_mint_prints_the_plaintext_once_and_sends_only_its_hash(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        result = world.run(_mint())

    assert result.exit_code == 0, result.output
    found = _PLAINTEXT.findall(result.output)
    assert len(found) == 1, result.output
    plaintext = found[0]
    assert result.stdout.count(plaintext) == 1
    (register,) = world.bus.on(_REGISTER)
    assert (register.command_topic, register.response_topic) == _REGISTER
    assert register.node_id == "node_tenant_api_key_effect"
    assert (
        register.payload["key_hash"] == hashlib.sha256(plaintext.encode()).hexdigest()
    )
    assert plaintext not in json.dumps([c.payload for c in world.bus.calls])
    assert register.payload["tenant_id"] == str(_TENANT_ID)
    assert register.payload["name"] == "ci-key"
    assert set(register.payload) == {
        "correlation_id",
        "tenant_id",
        "name",
        "key_hash",
        "expires_at",
    }
    for record in caplog.records:
        assert plaintext not in record.getMessage()
    assert world.store.secrets == {}


def test_mint_reads_the_tenant_on_the_read_topic_pair_before_it_registers(
    world: World,
) -> None:
    world.run(_mint())

    assert [(c.command_topic, c.response_topic) for c in world.bus.calls] == [
        _READ,
        _REGISTER,
    ]


def test_mint_expiry_defaults_to_72_hours_and_takes_expires_in(world: World) -> None:
    before = datetime.now(UTC)
    world.run(_mint())
    world.run(_mint("--expires-in", "30m"))

    default, short = [c.payload["expires_at"] for c in world.bus.on(_REGISTER)]
    default_at = datetime.fromisoformat(str(default))
    short_at = datetime.fromisoformat(str(short))
    assert (
        timedelta(hours=71, minutes=59)
        < default_at - before
        < timedelta(hours=72, minutes=1)
    )
    assert timedelta(minutes=29) < short_at - before < timedelta(minutes=31)


def test_mint_refuses_a_malformed_expiry_and_sends_nothing(world: World) -> None:
    result = world.run(_mint("--expires-in", "soon"))

    assert result.exit_code != 0
    assert world.bus.calls == []


def test_mint_prints_the_plaintext_after_a_confirm_on_a_tty(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_tenant, "_stdout_is_tty", lambda: True)

    declined = world.run(_mint(), input="n\n")
    accepted = world.run(_mint(), input="y\n")

    assert _PLAINTEXT.findall(declined.output) == []
    assert declined.exit_code == 0
    assert len(_PLAINTEXT.findall(accepted.output)) == 1


def test_mint_prints_nothing_secret_when_the_registration_is_refused(
    world: World,
) -> None:
    world.bus.script[_REGISTER[0]] = [_registered("refused_name_conflict")]

    result = world.run(_mint())

    assert result.exit_code == 1
    assert "refused_name_conflict" in result.output
    assert _PLAINTEXT.findall(result.output) == []
    assert len(world.bus.on(_REGISTER)) == 1


def test_mint_refuses_a_tenant_that_is_not_found_before_any_register(
    world: World,
) -> None:
    world.bus.script[_READ[0]] = [
        {
            "command_id": str(uuid4()),
            "outcome": "not_found",
            "detail": "no such tenant",
            "emitted_at": "2026-11-22T00:00:01Z",
        }
    ]

    result = world.run(_mint("--store-dev-secret", "--lane", "dev"))

    assert result.exit_code == 1
    assert world.bus.on(_REGISTER) == []
    assert world.store.secrets == {}


# --------------------------------------------------------------------------- the timeout resend


def test_a_first_dispatch_that_timed_out_after_committing_is_reported_registered(
    world: World,
) -> None:
    world.bus.script[_REGISTER[0]] = [None, _registered("already_registered")]

    result = world.run(_mint())

    assert result.exit_code == 0, result.output
    first, second = world.bus.on(_REGISTER)
    assert first.payload == second.payload
    assert "already_registered" in result.output
    assert len(_PLAINTEXT.findall(result.output)) == 1


def test_a_second_timeout_prints_nothing_secret_and_says_to_run_key_list(
    world: World,
) -> None:
    world.bus.script[_REGISTER[0]] = [None]

    result = world.run(_mint())

    assert result.exit_code == 1
    assert len(world.bus.on(_REGISTER)) == 2
    assert _PLAINTEXT.findall(result.output) == []
    assert "key list" in result.output
    assert "indeterminate" in result.output


def test_a_refusal_causes_no_resend(world: World) -> None:
    world.bus.script[_REGISTER[0]] = [_registered("refused_tenant_not_active")]

    result = world.run(_mint())

    assert result.exit_code == 1
    assert len(world.bus.on(_REGISTER)) == 1


# --------------------------------------------------------------------------- --store-dev-secret


def test_store_dev_secret_writes_create_only_then_registers_and_prints_the_path(
    world: World,
) -> None:
    result = world.run(_mint("--store-dev-secret", "--lane", "dev"))

    assert result.exit_code == 0, result.output
    (register,) = world.bus.on(_REGISTER)
    key_hash = str(register.payload["key_hash"])
    path = f"/tenant-gateway-keys/{_SLUG}.ci-key.{key_hash[:8]}"
    assert list(world.store.secrets) == [path]
    plaintext = world.store.secrets[path]
    assert hashlib.sha256(plaintext.encode()).hexdigest() == key_hash
    assert plaintext.startswith(_PREFIX)
    assert plaintext not in result.output
    assert path in result.output
    assert plaintext not in json.dumps([c.payload for c in world.bus.calls])


def test_store_dev_secret_takes_the_lane_from_the_environment(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_LANE", "dev")

    result = world.run(_mint("--store-dev-secret"))

    assert result.exit_code == 0, result.output
    assert len(world.store.secrets) == 1


@pytest.mark.parametrize(
    ("env_lane", "flag_lane", "api_key_lane", "node_store_env", "store_env", "outcome"),
    [
        pytest.param(
            "other-lane",
            None,
            "dev",
            "dev",
            "dev",
            "refused_not_dev",
            id="command-lane-env",
        ),
        pytest.param(
            None,
            "other-lane",
            "dev",
            "dev",
            "dev",
            "refused_not_dev",
            id="command-lane-flag",
        ),
        pytest.param(
            "dev",
            None,
            "other-lane",
            "dev",
            "dev",
            "refused_not_dev",
            id="overlay-lane",
        ),
        pytest.param(
            None, None, "dev", "dev", "dev", "refused_not_dev", id="no-lane-anywhere"
        ),
        pytest.param(
            "dev", None, None, "dev", "dev", "refused_not_dev", id="overlay-has-no-lane"
        ),
        pytest.param(
            "dev",
            None,
            "dev",
            "dev",
            "prod",
            "refused_store_not_dev",
            id="store-environment-differs",
        ),
        pytest.param(
            "dev",
            None,
            "dev",
            "dev",
            None,
            "refused_store_not_dev",
            id="store-environment-unset",
        ),
        pytest.param(
            "dev",
            None,
            "dev",
            None,
            "dev",
            "refused_store_not_dev",
            id="overlay-names-no-store-environment",
        ),
    ],
)
def test_store_dev_secret_is_refused_before_any_store_or_bus_call(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env_lane: str | None,
    flag_lane: str | None,
    api_key_lane: str | None,
    node_store_env: str | None,
    store_env: str | None,
    outcome: str,
) -> None:
    root = tmp_path / "refusal-overlays"
    _write_overlays(root, api_key_lane=api_key_lane, store_env=node_store_env)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    if env_lane is not None:
        monkeypatch.setenv("ONEX_LANE", env_lane)
    monkeypatch.setattr(
        cli_tenant, "_load_store_overlay", lambda: _store_overlay(store_env)
    )
    handlers_made: list[object] = []
    monkeypatch.setattr(
        cli_tenant,
        "_make_secret_handler",
        lambda overlay: handlers_made.append(overlay),
    )
    args = _mint("--store-dev-secret")
    if flag_lane is not None:
        args += ["--lane", flag_lane]

    result = world.run(args)

    assert result.exit_code == 1
    assert outcome in result.output
    assert world.bus.calls == []
    assert handlers_made == []
    assert world.store.secrets == {}
    assert _PLAINTEXT.findall(result.output) == []


def test_a_store_that_already_holds_the_name_gives_no_registration(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = _Store(existing=True)
    monkeypatch.setattr(
        cli_tenant,
        "_make_secret_handler",
        lambda overlay: HandlerSecretStore(store=held, overlay=overlay),
    )

    result = world.run(_mint("--store-dev-secret", "--lane", "dev"))

    assert result.exit_code == 1
    assert "exists" in result.output
    assert world.bus.on(_REGISTER) == []
    assert held.secrets == {}


def test_a_registration_failure_after_the_store_write_prints_the_orphaned_path(
    world: World,
) -> None:
    world.bus.script[_REGISTER[0]] = [_registered("refused_hash_collision")]

    result = world.run(_mint("--store-dev-secret", "--lane", "dev"))

    assert result.exit_code == 1
    (path,) = world.store.secrets
    assert path in result.output
    assert "orphan" in result.output
    assert world.store.secrets[path] not in result.output


def test_store_dev_secret_resend_after_a_timeout_reports_the_path_as_usable(
    world: World,
) -> None:
    world.bus.script[_REGISTER[0]] = [None, _registered("already_registered")]

    result = world.run(_mint("--store-dev-secret", "--lane", "dev"))

    assert result.exit_code == 0, result.output
    (path,) = world.store.secrets
    assert path in result.output
    assert len(world.bus.on(_REGISTER)) == 2
    assert world.store.secrets[path] not in result.output


def test_a_second_timeout_with_a_stored_secret_reports_it_indeterminate_with_the_path(
    world: World,
) -> None:
    world.bus.script[_REGISTER[0]] = [None]

    result = world.run(_mint("--store-dev-secret", "--lane", "dev"))

    assert result.exit_code == 1
    (path,) = world.store.secrets
    assert path in result.output
    assert "indeterminate" in result.output


# --------------------------------------------------------------------------- key revoke


def test_revoke_publishes_on_the_revoke_topic_pair(world: World) -> None:
    world.bus.script[_REVOKE[0]] = [
        {
            "correlation_id": str(uuid4()),
            "tenant_id": str(_TENANT_ID),
            "credential_id": str(_KEY_ID),
            "outcome": "revoked",
        }
    ]

    result = world.run(["key", "revoke", "--tenant", _SLUG, "--key-id", str(_KEY_ID)])

    assert result.exit_code == 0, result.output
    (call,) = world.bus.on(_REVOKE)
    assert call.response_topic == _REVOKE[1]
    assert set(call.payload) == {"correlation_id", "tenant_id", "credential_id"}
    assert call.payload["tenant_id"] == str(_TENANT_ID)
    assert call.payload["credential_id"] == str(_KEY_ID)
    assert "revoked" in result.output


def test_revoke_not_found_exits_one(world: World) -> None:
    world.bus.script[_REVOKE[0]] = [
        {
            "correlation_id": str(uuid4()),
            "tenant_id": str(_TENANT_ID),
            "credential_id": str(_KEY_ID),
            "outcome": "not_found",
        }
    ]

    result = world.run(
        ["key", "revoke", "--tenant", str(_TENANT_ID), "--key-id", str(_KEY_ID)]
    )

    assert result.exit_code == 1
    assert "not_found" in result.output


# --------------------------------------------------------------------------- the surface


def test_help_lists_create_show_and_key_and_key_help_lists_mint_and_revoke() -> None:
    top = CliRunner().invoke(tenant_group, ["--help"])
    key = CliRunner().invoke(tenant_group, ["key", "--help"])

    assert top.exit_code == 0
    assert key.exit_code == 0
    for command in ("create", "show", "key"):
        assert re.search(rf"^\s+{command}\b", top.output, re.MULTILINE), top.output
    for command in ("mint", "revoke"):
        assert re.search(rf"^\s+{command}\b", key.output, re.MULTILINE), key.output
    assert "list" not in key.output.split("Commands:")[1]


def test_the_module_names_no_topic_that_a_contract_does_not_declare() -> None:
    source = Path(cli_tenant.__file__).read_text(encoding="utf-8")

    assert "onex.cmd." not in source
    assert "onex.evt." not in source
    assert "subscribe_topics" not in source


def test_every_dispatch_is_a_call_of_the_public_publish_and_poll() -> None:
    from omnibase_core.cli import cli_run_node

    assert cli_tenant.publish_and_poll is cli_run_node.publish_and_poll
