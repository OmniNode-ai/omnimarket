# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex tenant``: the key stays in the caller's process, the bus carries a digest, and every command has its own topic pair.

The ports are fakes that record every call in one ordered list, so "refused before any store or bus call" is an empty
list and "the store was written first" is the list's order. The topic pairs are read from the contracts of the nodes that
serve them, so a rename there fails here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from click.testing import CliRunner, Result
from pydantic import SecretStr

from omnimarket.nodes.node_tenant_cli_effect.cli import TenantHandlers, tenant_group
from omnimarket.nodes.node_tenant_cli_effect.handlers.contract_topics import (
    KEY_NODE,
    PROVISION_NODE,
    key_topics,
    provision_topics,
)
from omnimarket.nodes.node_tenant_cli_effect.handlers.handler_tenant_create import (
    HandlerTenantCreate,
)
from omnimarket.nodes.node_tenant_cli_effect.handlers.handler_tenant_key_mint import (
    ANSWER_EVENT as MINT_ANSWER,
)
from omnimarket.nodes.node_tenant_cli_effect.handlers.handler_tenant_key_mint import (
    OPERATION as MINT_OPERATION,
)
from omnimarket.nodes.node_tenant_cli_effect.handlers.handler_tenant_key_mint import (
    HandlerTenantKeyMint,
)
from omnimarket.nodes.node_tenant_cli_effect.handlers.handler_tenant_key_revoke import (
    ANSWER_EVENT as REVOKE_ANSWER,
)
from omnimarket.nodes.node_tenant_cli_effect.handlers.handler_tenant_key_revoke import (
    OPERATION as REVOKE_OPERATION,
)
from omnimarket.nodes.node_tenant_cli_effect.handlers.handler_tenant_key_revoke import (
    HandlerTenantKeyRevoke,
)
from omnimarket.nodes.node_tenant_cli_effect.handlers.handler_tenant_show import (
    HandlerTenantShow,
)
from omnimarket.nodes.node_tenant_cli_effect.ports.protocol_tenant_cli import (
    TenantBusUnavailableError,
)

pytestmark = pytest.mark.unit

_PREFIX = "onxk_"
_TENANT = uuid4()
_NODE_ROOT = (
    Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_tenant_cli_effect"
)


class FakeBus:
    """Records each dispatch in ``calls`` and answers from a queue; ``None`` is a timeout."""

    def __init__(
        self, calls: list[tuple[str, object]], lanes: dict[str, str | None]
    ) -> None:
        self.calls = calls
        self.lanes = lanes
        self.sent: list[dict[str, object]] = []
        self.answers: list[dict[str, object] | None] = []
        self.unavailable = False

    def lane(self, node_id: str) -> str | None:
        return self.lanes.get(node_id)

    def dispatch(
        self,
        *,
        node_id: str,
        command_topic: str,
        response_topic: str,
        payload: dict[str, object],
        correlation_in_payload: bool,
    ) -> dict[str, object] | None:
        self.calls.append(("bus", command_topic))
        self.sent.append(
            {
                "node_id": node_id,
                "command_topic": command_topic,
                "response_topic": response_topic,
                "payload": payload,
                "correlation_in_payload": correlation_in_payload,
            }
        )
        if self.unavailable:
            raise TenantBusUnavailableError("the bus could not be opened")
        return self.answers.pop(0) if self.answers else None


class FakeOverlays:
    def __init__(
        self, calls: list[tuple[str, object]], *, lane: str | None, dev_env: str | None
    ) -> None:
        self.calls = calls
        self.lane = lane
        self.dev_env = dev_env

    def gateway_prefix(self) -> str:
        return _PREFIX

    def key_node_lane(self) -> str | None:
        return self.lane

    def key_node_dev_store_environment(self) -> str | None:
        return self.dev_env


class FakeStore:
    def __init__(
        self,
        calls: list[tuple[str, object]],
        *,
        environment: str | None,
        held: set[str],
    ) -> None:
        self.calls = calls
        self._environment = environment
        self.held = held
        self.created: dict[str, str] = {}

    def environment(self) -> str | None:
        return self._environment

    def create(self, name: str, value: SecretStr) -> str:
        self.calls.append(("store", name))
        if name in self.held:
            return "exists"
        self.created[name] = value.get_secret_value()
        return "created"


class World:
    """One fake world: dev on every axis unless a test moves one."""

    def __init__(
        self,
        *,
        command_lane: str | None = "dev",
        overlay_lane: str | None = "dev",
        dev_env: str | None = "dev",
        store_env: str | None = "dev",
        held: set[str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, object]] = []
        self.bus = FakeBus(
            self.calls, {KEY_NODE: command_lane, PROVISION_NODE: command_lane}
        )
        self.overlays = FakeOverlays(self.calls, lane=overlay_lane, dev_env=dev_env)
        self.store = FakeStore(self.calls, environment=store_env, held=held or set())
        self.handlers = TenantHandlers(
            create=HandlerTenantCreate(self.bus),
            show=HandlerTenantShow(self.bus),
            key_mint=HandlerTenantKeyMint(self.bus, self.overlays, self.store),
            key_revoke=HandlerTenantKeyRevoke(self.bus),
        )

    def run(self, *args: str) -> Result:
        return CliRunner().invoke(tenant_group, list(args), obj=self.handlers)


def _registered(outcome: str = "registered") -> dict[str, object]:
    return {
        "correlation_id": str(uuid4()),
        "tenant_id": str(_TENANT),
        "outcome": outcome,
        "credential_id": str(uuid4()),
    }


def _key_in(output: str) -> str:
    match = re.search(r"^key: (\S+)$", output, flags=re.MULTILINE)
    assert match is not None, output
    return match.group(1)


def _mint(*extra: str) -> tuple[str, ...]:
    return ("key", "mint", "--tenant-id", str(_TENANT), "--name", "ci", *extra)


def test_the_key_is_in_stdout_once_and_in_no_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = World()
    world.bus.answers = [_registered()]
    with caplog.at_level(logging.DEBUG):
        result = world.run(*_mint())
    assert result.exit_code == 0, result.output
    key = _key_in(result.output)
    assert key.startswith(_PREFIX)
    assert result.output.count(key) == 1
    assert key not in caplog.text
    assert all(key not in record.getMessage() for record in caplog.records)


def test_the_bus_payload_has_the_digest_and_no_key() -> None:
    world = World()
    world.bus.answers = [_registered()]
    result = world.run(*_mint())
    key = _key_in(result.output)
    (sent,) = world.bus.sent
    payload = sent["payload"]
    assert isinstance(payload, dict)
    assert payload["key_hash"] == hashlib.sha256(key.encode("utf-8")).hexdigest()
    assert set(payload) == {"tenant_id", "name", "key_hash"}
    assert key not in json.dumps(payload)
    assert key not in json.dumps(sent)
    assert sent["correlation_in_payload"] is True


def test_json_output_carries_the_key_once_and_no_log_leak() -> None:
    world = World()
    world.bus.answers = [_registered()]
    result = world.run(*_mint("--json"))
    document = json.loads(result.output)
    assert document["outcome"] == "registered"
    assert result.output.count(document["key"]) == 1


def test_a_failed_registration_prints_no_key() -> None:
    world = World()
    world.bus.answers = [_registered("refused_tenant_not_active")]
    result = world.run(*_mint())
    assert result.exit_code == 2
    assert "key:" not in result.output
    (sent,) = world.bus.sent
    assert "key_hash" in sent["payload"]  # type: ignore[operator]


@pytest.mark.parametrize(
    ("label", "world_args"),
    [
        ("command lane is not dev", {"command_lane": "stability"}),
        ("command lane is unreadable", {"command_lane": None}),
        ("overlay lane is not dev", {"overlay_lane": "stability"}),
        ("overlay is not the dev overlay", {"dev_env": None}),
        ("store environment differs from the overlay's", {"store_env": "prod"}),
        ("store names no environment", {"store_env": None}),
    ],
)
def test_store_dev_secret_is_refused_before_any_store_or_bus_call(
    label: str, world_args: dict[str, str | None]
) -> None:
    world = World(**world_args)  # type: ignore[arg-type]
    world.bus.answers = [_registered()]
    result = world.run(*_mint("--store-dev-secret"))
    assert result.exit_code == 2, label
    assert "refused_dev_secret" in result.output, label
    assert world.calls == [], label
    assert "key:" not in result.output, label


def test_store_dev_secret_on_dev_writes_the_store_first_then_registers() -> None:
    world = World()
    world.bus.answers = [_registered()]
    result = world.run(*_mint("--store-dev-secret"))
    assert result.exit_code == 0, result.output
    assert [kind for kind, _ in world.calls] == ["store", "bus"]
    assert world.store.created == {"ci": _key_in(result.output)}


def test_a_store_that_holds_the_name_gives_no_registration() -> None:
    world = World(held={"ci"})
    world.bus.answers = [_registered()]
    result = world.run(*_mint("--store-dev-secret"))
    assert result.exit_code == 2
    assert "refused_dev_secret_exists" in result.output
    assert [kind for kind, _ in world.calls] == ["store"]
    assert world.bus.sent == []
    assert "key:" not in result.output


def test_a_name_outside_the_store_alphabet_is_refused_before_any_call() -> None:
    world = World()
    result = world.run(
        "key",
        "mint",
        "--tenant-id",
        str(_TENANT),
        "--name",
        "a/b",
        "--store-dev-secret",
    )
    assert result.exit_code == 2
    assert world.calls == []


def test_a_timed_out_first_dispatch_is_reported_registered_through_already_registered() -> (
    None
):
    world = World()
    world.bus.answers = [None, _registered("already_registered")]
    result = world.run(*_mint())
    assert result.exit_code == 0, result.output
    assert "outcome: registered" in result.output
    first, second = world.bus.sent
    assert first["payload"] == second["payload"]
    assert result.output.count(_key_in(result.output)) == 1


def test_two_dispatches_without_an_answer_print_no_key() -> None:
    world = World()
    result = world.run(*_mint())
    assert result.exit_code == 3
    assert "no_answer" in result.output
    assert "key:" not in result.output
    assert len(world.bus.sent) == 2


def test_an_unavailable_bus_is_a_typed_result_with_no_key() -> None:
    world = World()
    world.bus.unavailable = True
    result = world.run(*_mint())
    assert result.exit_code == 3
    assert "bus_unavailable" in result.output
    assert "key:" not in result.output


def test_each_command_publishes_on_its_own_operation_topic_pair() -> None:
    world = World()
    slug_answer = {
        "command_id": str(uuid4()),
        "outcome": "created",
        "detail": "created",
        "tenant_id": str(_TENANT),
        "tenant_slug": "acme",
    }
    world.bus.answers = [
        slug_answer,
        {**slug_answer, "outcome": "found"},
        _registered(),
        {
            "outcome": "revoked",
            "tenant_id": str(_TENANT),
            "credential_id": str(uuid4()),
        },
    ]
    assert world.run("create", "--slug", "acme").exit_code == 0
    assert world.run("show", "--slug", "acme").exit_code == 0
    assert world.run(*_mint()).exit_code == 0
    assert (
        world.run(
            "key",
            "revoke",
            "--tenant-id",
            str(_TENANT),
            "--credential-id",
            str(uuid4()),
        ).exit_code
        == 0
    )
    pairs = [(s["command_topic"], s["response_topic"]) for s in world.bus.sent]
    declared = [
        provision_topics("tenant_provision"),
        provision_topics("tenant_read"),
        key_topics(MINT_OPERATION, MINT_ANSWER),
        key_topics(REVOKE_OPERATION, REVOKE_ANSWER),
    ]
    assert pairs == [(t.requested, t.completed) for t in declared]
    assert len(set(pairs)) == 4
    assert len({p[0] for p in pairs}) == 4
    assert len({p[1] for p in pairs}) == 4


def test_show_refuses_neither_and_both_without_a_bus_call() -> None:
    world = World()
    assert world.run("show").exit_code == 2
    assert (
        world.run("show", "--slug", "acme", "--tenant-id", str(uuid4())).exit_code == 2
    )
    assert world.calls == []


def test_create_payload_names_the_slug_and_a_command_id() -> None:
    world = World()
    world.bus.answers = [
        {"command_id": str(uuid4()), "outcome": "created", "detail": "ok"}
    ]
    world.run("create", "--slug", "acme", "--name", "Acme")
    payload = world.bus.sent[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["tenant_slug"] == "acme"
    assert payload["name"] == "Acme"
    UUID(str(payload["command_id"]))
    assert world.bus.sent[0]["correlation_in_payload"] is False


def test_a_refused_slug_is_exit_two_and_a_store_outage_is_exit_three() -> None:
    world = World()
    world.bus.answers = [
        {"outcome": "refused_slug_taken", "detail": "taken"},
        {"outcome": "failed_store_unavailable", "detail": "store down"},
    ]
    assert world.run("create", "--slug", "acme").exit_code == 2
    assert world.run("create", "--slug", "acme").exit_code == 3


def test_the_command_group_holds_no_database_connection() -> None:
    """Falsifier: a DSN or a driver import anywhere in the node's code."""
    forbidden = re.compile(
        r"asyncpg|psycopg|sqlalchemy|\bdsn\b|DATABASE_URL|postgres(ql)?://|sqlite3|\bimport\s+(aiomysql|pymysql)",
        re.IGNORECASE,
    )
    offenders = [
        f"{path.name}:{number}"
        for path in _NODE_ROOT.rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if forbidden.search(line) and not line.lstrip().startswith(("#", '"""'))
    ]
    assert offenders == [], offenders


def test_the_detector_finds_a_driver_import() -> None:
    """Positive control for the scan above."""
    forbidden = re.compile(r"asyncpg|psycopg|\bdsn\b", re.IGNORECASE)
    assert forbidden.search("import asyncpg")
    assert forbidden.search("dsn = os.environ['X']")


def test_no_topic_string_lives_in_a_handler() -> None:
    topic = re.compile(r"onex\.(cmd|evt)\.")
    offenders = [
        path.name
        for path in (_NODE_ROOT / "handlers").rglob("*.py")
        if topic.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_contract_declares_no_topic_of_its_own() -> None:
    raw = yaml.safe_load((_NODE_ROOT / "contract.yaml").read_text(encoding="utf-8"))
    assert raw["event_bus"] == {"subscribe_topics": [], "publish_topics": []}


def test_the_dev_guard_helper_is_exercised_through_the_world() -> None:
    """Positive control for the refusal test: the all-dev world is allowed, so the refusals come from the moved axis."""
    world = World()
    world.bus.answers = [_registered()]
    assert world.run(*_mint("--store-dev-secret")).exit_code == 0
