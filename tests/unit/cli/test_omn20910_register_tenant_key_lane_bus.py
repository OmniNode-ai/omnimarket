# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""register-tenant-key publishes on the host's declared bus lane (OMN-20910).

A lab host carries no KAFKA_BOOTSTRAP_SERVERS, so registering a key there used
to fail at the publish step. The bound host's row in the lab host table names
the bus lane it publishes on (``ledger_bus_lane``); with no broker in the
environment the event goes out on that lane, opened through the lab-run lane
resolver. A broker the environment names keeps the publisher's own path.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.cli import cli_secret
from omnimarket.delegated_test_loop import lane_bus
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.projection import credential_publisher

pytestmark = pytest.mark.unit
_VALUE = "sk-test-lane-bus-secret-never-published"
_ENVS = (
    "ONEX_LAB_RUN_HOSTS",
    "OMNI_HOME",
    "OMNIBASE_PATH",
    "ONEX_LANE_HOST",
    "ONEX_REMOTE_LANE_HOST",
    "KAFKA_BOOTSTRAP_SERVERS",
    "KAFKA_BROKER",
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
def lane_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str | None], FakeBus]:
    store = LocalByokCredentialStore(db_path=tmp_path / "delegation.sqlite")
    monkeypatch.setattr(cli_secret, "_tenant_key_store", lambda: store)
    monkeypatch.setattr(cli_secret, "_stdin_is_tty", lambda: False)
    for name in _ENVS:
        monkeypatch.delenv(name, raising=False)
    table = tmp_path / "lab_run_hosts.yaml"
    table.write_text(
        "hosts:\n"
        "  - name: h202\n"
        "    target: lab-host-202\n"
        "    ledger_bus_lane: dev\n"
        "  - name: h201\n"
        "    target: lab-host-201\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(table))
    monkeypatch.setattr(cli_secret.socket, "gethostname", lambda: "unlisted.local")
    opened: list[str | None] = []
    fake = FakeBus()

    @contextlib.asynccontextmanager
    async def fake_open(
        *, bus: str, lane: str | None, kafka_bootstrap: str | None, workspace: Any
    ) -> AsyncIterator[FakeBus]:
        opened.append(lane)
        assert bus == "kafka"
        assert kafka_bootstrap is None
        yield fake

    monkeypatch.setattr(lane_bus, "open_lab_run_bus", fake_open)
    return opened, fake


def _register(*extra: str) -> Any:
    return CliRunner().invoke(
        cli_secret.secret_group,
        [
            "register-tenant-key",
            "glm",
            "--tenant",
            "omninode",
            "--plan",
            "general_api",
            "--expires",
            "2099-01-01",
            *extra,
        ],
        input=f"{_VALUE}\n",
    )


def test_no_broker_in_the_environment_publishes_on_the_host_row_lane(
    lane_opens: tuple[list[str | None], FakeBus],
) -> None:
    opened, bus = lane_opens
    result = _register("--host", "h202")
    assert result.exit_code == 0, result.output
    assert opened == ["dev"]
    assert len(bus.events) == 1
    assert json.loads(bus.events[0])["payload"]["bound_host"] == "h202"
    assert _VALUE not in result.output


def test_a_broker_in_the_environment_keeps_the_publisher_path(
    lane_opens: tuple[list[str | None], FakeBus], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened, _lane = lane_opens
    env_bus = FakeBus()
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "broker.example:9092")
    monkeypatch.setattr(credential_publisher, "_build_event_bus", lambda: env_bus)
    result = _register("--host", "h202")
    assert result.exit_code == 0, result.output
    assert opened == []
    assert len(env_bus.events) == 1


def test_a_row_with_no_lane_keeps_the_publisher_refusal(
    lane_opens: tuple[list[str | None], FakeBus], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened, _lane = lane_opens

    def refuse() -> FakeBus:
        raise RuntimeError("KAFKA_BOOTSTRAP_SERVERS (or KAFKA_BROKER) is required")

    monkeypatch.setattr(credential_publisher, "_build_event_bus", refuse)
    result = _register("--host", "h201")
    assert result.exit_code != 0
    assert opened == []


def test_an_unopenable_lane_is_refused_by_name(
    lane_opens: tuple[list[str | None], FakeBus], monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextlib.asynccontextmanager
    async def broken(
        *, bus: str, lane: str | None, kafka_bootstrap: str | None, workspace: Any
    ) -> AsyncIterator[FakeBus]:
        raise lane_bus.LabRunBusError("lane 'dev' resolves no broker login")
        yield FakeBus()

    monkeypatch.setattr(lane_bus, "open_lab_run_bus", broken)
    result = _register("--host", "h202")
    assert result.exit_code != 0
    assert "declared bus lane 'dev' could not be opened" in result.output
    assert _VALUE not in result.output
