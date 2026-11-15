# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex models review-gateway`` serves on the host's declared bus lane (OMN-19998).

The launchd unit (ai.omninode.review-gateway) passes no KAFKA_BOOTSTRAP_SERVERS
and a lab host never carries one, so serving used to exit 1 in a crash loop.
The bound host's lab host table row names the bus lane it publishes on; with no
broker in the environment the handler is built on that lane's bus. A broker the
environment names keeps the handler's own Kafka path, and a host that declares
no lane keeps the refusal.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.cli import cli_secret
from omnimarket.delegated_test_loop import lane_bus
from omnimarket.handlers.handler_lab_host_bus_lane import lab_host_bus_lane
from omnimarket.nodes.node_claude_review_gateway_effect.handlers.handler_claude_review_gateway import (
    HandlerClaudeReviewGateway,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def gateway_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A quiet lab host: no broker env, bound host h200, both overlay files."""
    privacy = tmp_path / "privacy.yaml"
    privacy.write_text(
        "cloud_review_refused_repositories:\n  - secret_repo\n", encoding="utf-8"
    )
    policy = tmp_path / "gateway-policy.yaml"
    policy.write_text("key_set_aside_ttl_seconds: 17\n", encoding="utf-8")
    monkeypatch.setenv("ONEX_REVIEW_GATEWAY_OVERLAY", str(policy))
    monkeypatch.setattr(cli_secret, "_resolve_bound_host", lambda _host: "h200")
    monkeypatch.setattr(
        cli_secret,
        "_tenant_key_store",
        lambda: SimpleNamespace(db_path=tmp_path / "keys.db"),
    )
    for name in ("KAFKA_BOOTSTRAP_SERVERS", "KAFKA_BROKER"):
        monkeypatch.delenv(name, raising=False)
    return privacy


def _invoke(privacy_overlay: Path) -> Any:
    return CliRunner().invoke(
        cli_secret.models_group,
        [
            "review-gateway",
            "--tenant",
            "t",
            "--model",
            "claude-sonnet-5-5",
            "--bind",
            "127.0.0.1:8140",
            "--privacy-overlay",
            str(privacy_overlay),
        ],
    )


def test_no_broker_serves_on_the_host_row_lane(
    gateway_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_secret, "_environment_names_broker", lambda: False)
    monkeypatch.setattr(cli_secret, "_lab_host_bus_lane", lambda _host: "dev")
    seen: list[object] = []
    opened: list[str | None] = []
    lane_bus_sentinel = SimpleNamespace()

    @contextlib.asynccontextmanager
    async def fake_open(
        *,
        bus: str,
        lane: str | None,
        kafka_bootstrap: str | None,
        workspace: Path | None,
    ) -> AsyncIterator[object]:
        opened.append(lane)
        assert bus == "kafka"
        assert kafka_bootstrap is None
        yield lane_bus_sentinel

    async def record_serve(
        self: HandlerClaudeReviewGateway,
        request: object,
        gateway: object,
    ) -> None:
        seen.append(self._event_bus)

    monkeypatch.setattr(HandlerClaudeReviewGateway, "_serve", record_serve)
    monkeypatch.setattr(lane_bus, "open_lab_run_bus", fake_open)
    result = _invoke(gateway_env)
    assert result.exit_code == 0, result.output
    assert opened == ["dev"]
    assert seen == [lane_bus_sentinel]


def test_a_broker_in_the_environment_keeps_the_handler_kafka_path(
    gateway_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_secret, "_environment_names_broker", lambda: True)
    monkeypatch.setattr(cli_secret, "_lab_host_bus_lane", lambda _host: "dev")
    seen: list[object] = []
    opened: list[str | None] = []

    @contextlib.asynccontextmanager
    async def fake_open(
        *,
        bus: str,
        lane: str | None,
        kafka_bootstrap: str | None,
        workspace: Path | None,
    ) -> AsyncIterator[object]:
        opened.append(lane)
        yield SimpleNamespace()

    async def record_handle(
        self: HandlerClaudeReviewGateway,
        request: object,
        **kwargs: object,
    ) -> None:
        seen.append(self._event_bus)

    monkeypatch.setattr(HandlerClaudeReviewGateway, "handle", record_handle)
    monkeypatch.setattr(lane_bus, "open_lab_run_bus", fake_open)
    result = _invoke(gateway_env)
    assert result.exit_code == 0, result.output
    assert opened == []
    assert seen == [None]


def test_a_host_that_declares_no_lane_keeps_the_refusal(
    gateway_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_secret, "_environment_names_broker", lambda: False)
    monkeypatch.setattr(cli_secret, "_lab_host_bus_lane", lambda _host: None)
    result = _invoke(gateway_env)
    assert result.exit_code != 0
    # The handler's ValueError is caught by the runner, not echoed, so the
    # refusal text is read from wherever this click version surfaces it.
    carried = f"{result.output}\n{result.stderr}\n{result.exception}"
    assert "review gateway requires KAFKA_BOOTSTRAP_SERVERS" in carried


def test_an_unopenable_lane_is_refused_by_name(
    gateway_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_secret, "_environment_names_broker", lambda: False)
    monkeypatch.setattr(cli_secret, "_lab_host_bus_lane", lambda _host: "dev")

    @contextlib.asynccontextmanager
    async def broken(
        *,
        bus: str,
        lane: str | None,
        kafka_bootstrap: str | None,
        workspace: Path | None,
    ) -> AsyncIterator[object]:
        raise lane_bus.LabRunBusError("no broker")
        yield SimpleNamespace()

    monkeypatch.setattr(lane_bus, "open_lab_run_bus", broken)
    result = _invoke(gateway_env)
    assert result.exit_code != 0
    assert "declared bus lane 'dev' could not be opened" in result.output


def test_seam_the_real_lane_opener_still_matches_the_helper_binding() -> None:
    """Seam: the helper binds the opener's arguments by declared order.

    ``_register_on_declared_bus`` refuses an opener whose first three parameters are not
    bus, lane and kafka_bootstrap or that does not take exactly four; the review gateway and
    the tenant-key registration share that helper, so a reshaped opener fails here first.
    """
    import inspect

    names = tuple(inspect.signature(lane_bus.open_lab_run_bus).parameters)
    assert names[:3] == ("bus", "lane", "kafka_bootstrap")
    assert len(names) == 4


def test_the_shared_lane_reader_reads_the_host_row(tmp_path: Path) -> None:
    """The shared reader returns the named row's lane, and none otherwise."""
    table = tmp_path / "hosts.yaml"
    table.write_text(
        "hosts:\n"
        "  - name: h200\n"
        "    ledger_bus_lane: dev\n"
        "  - name: h051\n"
        "    ledger_bus_lane: null\n",
        encoding="utf-8",
    )
    assert lab_host_bus_lane(table, "h200") == "dev"
    assert lab_host_bus_lane(table, "h051") is None
    assert lab_host_bus_lane(table, "h999") is None
    assert lab_host_bus_lane(None, "h200") is None
    assert lab_host_bus_lane(tmp_path / "no-such-table.yaml", "h200") is None
