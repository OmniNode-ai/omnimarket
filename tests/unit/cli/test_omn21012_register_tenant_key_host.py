# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A tenant key is bound to the lab host whose Claude account owns it (OMN-21012).

Operator ruling 2026-10-11T17:58:45Z: every lab machine has its own Claude Code
account, and each account's API key is bound to the machine it lives on, so
work sent from a host spends only that host's key. ``register-tenant-key``
records the host on the response, the credential-registered event and (through
the projection) the catalog row. These tests pin where the host comes from and
when a registration is refused before anything is stored.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import SecretStr, ValidationError

from omnimarket.cli import cli_secret
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.projection.credential_publisher import (
    ModelCredentialRegisteredEvent,
    ModelInferenceCredentialCreateRequest,
)

# OMN-21059: every shipped row has the customer choose the model; this file
# registers a key with no model, which a row reaches with that flag off.
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("catalogue_picks_models")]
_VALUE = "sk-test-host-bound-secret-never-published"
_TENANT = "omninode"
_HOST_ENVS = (
    "ONEX_LAB_RUN_HOSTS",
    "OMNI_HOME",
    "ONEX_LANE_HOST",
    "ONEX_REMOTE_LANE_HOST",
)


class FakeBus:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def publish_envelope(
        self, envelope: Any, topic: str, *, key: bytes | None = None
    ) -> None:
        self.events.append(envelope.model_dump_json())


@pytest.fixture
def intake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LocalByokCredentialStore, FakeBus]:
    store = LocalByokCredentialStore(db_path=tmp_path / "delegation.sqlite")
    bus = FakeBus()
    monkeypatch.setattr(cli_secret, "_tenant_key_store", lambda: store)
    monkeypatch.setattr(cli_secret, "_tenant_key_event_bus", lambda: bus)
    monkeypatch.setattr(cli_secret, "_stdin_is_tty", lambda: False)
    for name in _HOST_ENVS:
        monkeypatch.delenv(name, raising=False)
    return store, bus


@pytest.fixture
def lab_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    table = tmp_path / "lab_run_hosts.yaml"
    table.write_text(
        "hosts:\n"
        "  - name: h200\n"
        "    target: studio-host\n"
        "    local: true\n"
        "  - name: h201\n"
        "    target: lab-host-201\n"
        "  - name: h101\n"
        "    target: lab-host-101\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(table))
    monkeypatch.setattr(cli_secret.socket, "gethostname", lambda: "unlisted.local")
    return table


def _register(*extra: str) -> Any:
    return CliRunner().invoke(
        cli_secret.secret_group,
        [
            "register-tenant-key",
            "glm",
            "--tenant",
            _TENANT,
            "--plan",
            "general_api",
            "--expires",
            "2099-01-01",
            *extra,
        ],
        input=f"{_VALUE}\n",
    )


def _event_payload(bus: FakeBus) -> dict[str, Any]:
    assert len(bus.events) == 1
    payload: dict[str, Any] = json.loads(bus.events[0])["payload"]
    return payload


def test_register_tenant_key_host_records_the_named_host(
    intake: tuple[LocalByokCredentialStore, FakeBus], lab_table: Path
) -> None:
    store, bus = intake
    result = _register("--host", "h201")
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert output["bound_host"] == "h201"
    assert _event_payload(bus)["bound_host"] == "h201"
    assert asyncio.run(store.get_secret(output["api_key_ref"])) == _VALUE
    assert _VALUE not in result.output


@pytest.mark.parametrize("bad", ["h999", "H201", "lab-host-201", ""])
def test_register_tenant_key_host_refuses_a_host_not_in_the_table(
    intake: tuple[LocalByokCredentialStore, FakeBus], lab_table: Path, bad: str
) -> None:
    store, bus = intake
    result = _register("--host", bad)
    assert result.exit_code != 0
    assert "nothing was stored" in result.output.lower()
    assert _VALUE not in result.output
    assert bus.events == []
    assert asyncio.run(store.list_keys()) == []


def test_register_tenant_key_host_refuses_a_named_host_off_the_lab(
    intake: tuple[LocalByokCredentialStore, FakeBus],
) -> None:
    store, bus = intake
    result = _register("--host", "h201")
    assert result.exit_code != 0
    assert "no lab host table" in result.output
    assert bus.events == []
    assert asyncio.run(store.list_keys()) == []


def test_register_tenant_key_host_default_is_the_declared_lane_host(
    intake: tuple[LocalByokCredentialStore, FakeBus],
    lab_table: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, bus = intake
    monkeypatch.setenv("ONEX_LANE_HOST", "h101")
    result = _register()
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["bound_host"] == "h101"
    assert _event_payload(bus)["bound_host"] == "h101"


def test_register_tenant_key_host_default_matches_the_hostname_to_a_row(
    intake: tuple[LocalByokCredentialStore, FakeBus],
    lab_table: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, bus = intake
    monkeypatch.setattr(
        cli_secret.socket, "gethostname", lambda: "studio-host.example.lan"
    )
    result = _register()
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["bound_host"] == "h200"
    assert _event_payload(bus)["bound_host"] == "h200"


def test_register_tenant_key_host_default_is_unbound_off_the_lab(
    intake: tuple[LocalByokCredentialStore, FakeBus],
) -> None:
    _store, bus = intake
    result = _register()
    assert result.exit_code == 0, result.output
    assert "bound_host" not in json.loads(result.output)
    assert _event_payload(bus)["bound_host"] is None


def test_register_tenant_key_host_default_is_unbound_on_an_unlisted_machine(
    intake: tuple[LocalByokCredentialStore, FakeBus], lab_table: Path
) -> None:
    _store, bus = intake
    result = _register()
    assert result.exit_code == 0, result.output
    assert "bound_host" not in json.loads(result.output)
    assert _event_payload(bus)["bound_host"] is None


def test_register_tenant_key_host_default_refuses_an_undeclared_lane_host(
    intake: tuple[LocalByokCredentialStore, FakeBus],
    lab_table: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, bus = intake
    monkeypatch.setenv("ONEX_LANE_HOST", "h999")
    result = _register()
    assert result.exit_code != 0
    assert "ONEX_LANE_HOST" in result.output
    assert bus.events == []
    assert asyncio.run(store.list_keys()) == []


@pytest.mark.parametrize("bad", ["H201", "h201.local", "h_201", " h201", "-h201"])
def test_register_tenant_key_host_shape_is_refused_by_the_request_and_event(
    bad: str,
) -> None:
    with pytest.raises(ValidationError, match="bound_host"):
        ModelInferenceCredentialCreateRequest(
            name="k", provider="glm", key_value=SecretStr(_VALUE), bound_host=bad
        )
    with pytest.raises(ValidationError, match="bound_host"):
        ModelCredentialRegisteredEvent.model_validate(
            {
                "tenant_id": _TENANT,
                "provider": "glm",
                "name": "k",
                "api_key_ref": "cred_omninode_glm_" + "a" * 32,
                "bound_host": bad,
            }
        )
