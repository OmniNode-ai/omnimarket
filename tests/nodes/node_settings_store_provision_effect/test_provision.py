# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24052: the dev settings store provision node, against an in-memory Valkey and the real secret store contract.

Covers: the writer login is created with the stored password and authenticates; the password is never in a result or a log; a
second run changes nothing; a lane or role the contract does not declare is refused before any secret read or Valkey call; a
database another user holds is refused; a login holding another password is refused, never rotated.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_settings_store_provision_effect.handlers.handler_settings_store_provision import (
    HandlerSettingsStoreProvision,
)
from omnimarket.nodes.node_settings_store_provision_effect.models.model_settings_store_provision import (
    EnumSettingsStoreLogin,
    EnumSettingsStoreOutcome,
    ModelSettingsStoreProvisionCommand,
)
from omnimarket.nodes.node_settings_store_provision_effect.overlay import (
    ModelSettingsStoreProvisionOverlay,
)
from omnimarket.nodes.node_settings_store_provision_effect.protocols.protocol_valkey_acl_applier import (
    ValkeyUnavailableError,
)
from tests.nodes.node_stability_gateway_key_provision_effect.fakes import (
    FakeSecretStore,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
CONTRACT = ROOT / "node_settings_store_provision_effect/contract.yaml"
ADMIN_FOLDER, ADMIN_KEY, ADMIN_VALUE = "/shared/env", "VALKEY_ADMIN_NAME", "admin-pw"


def _intake(database: int) -> dict[str, str]:
    return {
        "ONEX_SETTINGS_STORE_KIND": "store_valkey",
        "ONEX_SETTINGS_STORE_ADDRESS": "valkey.test.invalid",
        "ONEX_SETTINGS_STORE_PORT": "6379",
        "ONEX_SETTINGS_STORE_DATABASE": str(database),
        "ONEX_SETTINGS_STORE_KEY_PREFIX": "t:settings",
        "ONEX_SETTINGS_STORE_LOGIN": "w",
        "ONEX_BROKER_KIND": "kafka",
        "ONEX_BROKER_ADDRESS": "broker.test.invalid:9092",
        "ONEX_BROKER_LOGIN": "b",
        "ONEX_INFISICAL_KIND": "infisical",
        "ONEX_INFISICAL_IDENTITY": "CLIENT_ID_NAME",
    }


def _overlay(
    lane: str = "dev", database: int = 7
) -> ModelSettingsStoreProvisionOverlay:
    return ModelSettingsStoreProvisionOverlay.model_validate(
        {
            "lane": lane,
            "store": {
                "address": "valkey.test.invalid",
                "port": 6379,
                "database": database,
                "key_prefix": "t:settings",
                "cache_key_pattern": "t:cache:*",
                "admin_login": "default",
                "admin_password_folder": ADMIN_FOLDER,
                "admin_password_key": ADMIN_KEY,
            },
            "bootstrap_intake": _intake(database),
            "logins": {
                "writer": {
                    "name": "w",
                    "password_key_id": "settings_store_writer_password",
                },
                "reader": {
                    "name": "r",
                    "password_key_id": "settings_store_reader_password",
                },
                "cache": {
                    "name": "c",
                    "password_key_id": "settings_store_cache_password",
                },
            },
        }
    )


class FakeValkey:
    """Users by name with their password and ACL row; keyspace counts; every call recorded."""

    def __init__(
        self,
        *,
        keyspace: Mapping[int, int] | None = None,
        users: dict[str, str] | None = None,
        stale_rows: frozenset[str] = frozenset(),
        unavailable: bool = False,
    ) -> None:
        self.keyspace = dict(keyspace or {0: 5})
        self.users = dict(users or {})
        # a user in stale_rows exists with a row that lacks +select, as the first rows valkey_acl.py rendered did
        self.rows: dict[str, tuple[str, ...]] = {
            name: ("ACL", "SETUSER", name, "reset", "on", "+get") for name in stale_rows
        }
        self.calls: list[str] = []
        self._unavailable = unavailable

    def _check(self, call: str) -> None:
        self.calls.append(call)
        if self._unavailable:
            raise ValkeyUnavailableError("ConnectionRefusedError")

    async def keyspace_counts(self) -> Mapping[int, int]:
        self._check("keyspace")
        return self.keyspace

    async def user_exists(self, login: str) -> bool:
        self._check("exists")
        return login in self.users

    async def set_user(self, command: tuple[str, ...]) -> None:
        self._check("set_user")
        assert command[:2] == ("ACL", "SETUSER")
        password = next(part[1:] for part in command if part.startswith(">"))
        self.users[command[2]] = password
        self.rows[command[2]] = command

    async def authenticates(self, login: str, password: str) -> bool:
        """A session on the store's database: the password is right and the row lets it SELECT."""
        return self.accepts_password_sync(
            login, password
        ) and "+select" in self.rows.get(login, ())

    def accepts_password_sync(self, login: str, password: str) -> bool:
        return self.users.get(login) == password

    async def accepts_password(self, login: str, password: str) -> bool:
        return self.accepts_password_sync(login, password)


def _handler(
    valkey: FakeValkey,
    store: FakeSecretStore,
    overlay: ModelSettingsStoreProvisionOverlay | None = None,
) -> HandlerSettingsStoreProvision:
    return HandlerSettingsStoreProvision(
        overlay=overlay or _overlay(),
        settings_store=store.reader,
        key_generator=store.generator,
        applier_factory=lambda _admin: valkey,
        contract_path=CONTRACT,
    )


def _store() -> FakeSecretStore:
    return FakeSecretStore({(ADMIN_FOLDER, ADMIN_KEY): ADMIN_VALUE})


async def test_writer_row_applied_and_login_authenticates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    valkey, store = FakeValkey(), _store()
    caplog.set_level(logging.DEBUG)
    result = await _handler(valkey, store).handle(
        ModelSettingsStoreProvisionCommand(lane="dev")
    )
    assert result.outcome is EnumSettingsStoreOutcome.PROVISIONED
    assert result.applied == ("w",)
    assert result.secret_key == "ONEX_SETTINGS_STORE_PASSWORD"
    value = store.values[("/shared/env", "ONEX_SETTINGS_STORE_PASSWORD")]
    assert valkey.users == {"w": value}
    assert "%RW~t:settings:*" in valkey.rows["w"]
    assert value not in result.model_dump_json()
    assert value not in caplog.text
    assert ("/shared/env", "ONEX_SETTINGS_STORE_PASSWORD") in store.writes


async def test_second_run_changes_nothing() -> None:
    valkey, store = FakeValkey(), _store()
    command = ModelSettingsStoreProvisionCommand(lane="dev")
    await _handler(valkey, store).handle(command)
    writes, row = list(store.writes), dict(valkey.rows)
    set_users = valkey.calls.count("set_user")
    result = await _handler(valkey, store).handle(command)
    assert result.outcome is EnumSettingsStoreOutcome.ALREADY_PROVISIONED
    assert result.unchanged == ("w",)
    assert store.writes == writes
    assert valkey.rows == row
    assert valkey.calls.count("set_user") == set_users


@pytest.mark.parametrize(
    ("command_lane", "overlay_lane"),
    [
        ("prod", "dev"),
        ("dev", "prod"),
        ("stability", "stability"),
        ("aws-dev", "aws-dev"),
    ],
)
async def test_undeclared_lane_refused_before_any_read_or_call(
    command_lane: str, overlay_lane: str
) -> None:
    valkey, store = FakeValkey(), _store()
    result = await _handler(valkey, store, _overlay(overlay_lane)).handle(
        ModelSettingsStoreProvisionCommand(lane=command_lane)
    )
    assert result.outcome is EnumSettingsStoreOutcome.REFUSED_LANE_NOT_ALLOWED
    assert valkey.calls == []
    assert store.touched() == set()


@pytest.mark.parametrize(
    "role", [EnumSettingsStoreLogin.READER, EnumSettingsStoreLogin.CACHE]
)
async def test_role_outside_consent_refused_before_any_read_or_call(
    role: EnumSettingsStoreLogin,
) -> None:
    valkey, store = FakeValkey(), _store()
    result = await _handler(valkey, store).handle(
        ModelSettingsStoreProvisionCommand(lane="dev", logins=(role,))
    )
    assert result.outcome is EnumSettingsStoreOutcome.REFUSED_LOGIN_NOT_DECLARED
    assert valkey.calls == []
    assert store.touched() == set()


async def test_database_held_by_another_user_refused() -> None:
    valkey, store = FakeValkey(keyspace={7: 3}), _store()
    result = await _handler(valkey, store).handle(
        ModelSettingsStoreProvisionCommand(lane="dev")
    )
    assert result.outcome is EnumSettingsStoreOutcome.REFUSED_DATABASE_IN_USE
    assert "set_user" not in valkey.calls
    assert valkey.users == {}


async def test_login_holding_another_password_is_not_rotated() -> None:
    valkey, store = FakeValkey(users={"w": "someone-elses"}), _store()
    result = await _handler(valkey, store).handle(
        ModelSettingsStoreProvisionCommand(lane="dev")
    )
    assert result.outcome is EnumSettingsStoreOutcome.REFUSED_LOGIN_HOLDS_OTHER_PASSWORD
    assert valkey.users == {"w": "someone-elses"}
    assert "set_user" not in valkey.calls


async def test_stale_row_with_the_stored_password_is_repaired_without_rotating() -> (
    None
):
    store = _store()
    probe = FakeValkey()
    await _handler(probe, store).handle(ModelSettingsStoreProvisionCommand(lane="dev"))
    stored = store.values[("/shared/env", "ONEX_SETTINGS_STORE_PASSWORD")]
    valkey = FakeValkey(users={"w": stored}, stale_rows=frozenset({"w"}))
    result = await _handler(valkey, store).handle(
        ModelSettingsStoreProvisionCommand(lane="dev")
    )
    assert result.outcome is EnumSettingsStoreOutcome.PROVISIONED
    assert valkey.users == {"w": stored}
    assert "+select" in valkey.rows["w"]


async def test_missing_admin_password_is_typed() -> None:
    valkey, store = FakeValkey(), FakeSecretStore()
    result = await _handler(valkey, store).handle(
        ModelSettingsStoreProvisionCommand(lane="dev")
    )
    assert result.outcome is EnumSettingsStoreOutcome.ADMIN_UNAVAILABLE
    assert valkey.calls == []


async def test_valkey_down_is_typed_and_nothing_is_applied() -> None:
    valkey, store = FakeValkey(unavailable=True), _store()
    result = await _handler(valkey, store).handle(
        ModelSettingsStoreProvisionCommand(lane="dev")
    )
    assert result.outcome is EnumSettingsStoreOutcome.STORE_UNAVAILABLE
    assert valkey.users == {}


def test_contract_and_secret_store_declare_what_the_overlay_names() -> None:
    declared = yaml.safe_load(CONTRACT.read_text("utf-8"))["dev_only"]
    assert declared == {"lanes": ["dev"], "logins": ["writer"]}
    keys = yaml.safe_load(
        (ROOT / "node_secret_store_effect/contract.yaml").read_text("utf-8")
    )["generated_keys"]
    overlay = _overlay()
    for login in (overlay.logins.writer, overlay.logins.reader, overlay.logins.cache):
        assert login.password_key_id in keys
    assert (
        keys["settings_store_writer_password"]["key"] == "ONEX_SETTINGS_STORE_PASSWORD"
    )


def test_intake_renders_to_a_bootstrap_file_the_store_wiring_parses() -> None:
    from omnimarket.handlers.overlay_store.bootstrap_file import (
        parse_bootstrap_file,
        render_bootstrap_file,
    )
    from omnimarket.nodes.node_settings_store_provision_effect.overlay import (
        render_intake,
    )

    text = render_intake(_overlay())
    values = dict(line.partition("=")[::2] for line in text.splitlines())
    assert parse_bootstrap_file(render_bootstrap_file(values)) == values
    assert values["ONEX_SETTINGS_STORE_LOGIN"] == "w"
    assert "password" not in text.lower()


def test_intake_that_differs_from_the_declared_store_is_refused() -> None:
    document = _overlay().model_dump()
    document["bootstrap_intake"]["ONEX_SETTINGS_STORE_DATABASE"] = "0"
    with pytest.raises(ValueError, match="differs from the store"):
        ModelSettingsStoreProvisionOverlay.model_validate(document)


def test_example_overlay_validates() -> None:
    example = ROOT / "node_settings_store_provision_effect/overlay.example.yaml"
    ModelSettingsStoreProvisionOverlay.model_validate(
        yaml.safe_load(example.read_text("utf-8"))
    )
