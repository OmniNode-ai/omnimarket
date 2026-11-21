# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-host serve of node_ci_checkout_effect: golden and error chains over the bus (OMN-24287).

Exemplar followed: tests/nodes/node_repo_state_query_effect (the serve test) and this node's golden chain test.
The serve is the seam between the bus and the handler: it answers its own host's requests, ignores another
host's, and ends an unreadable request on the failed terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    ProtocolLabRunBus,
    event_type_for,
)
from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
from omnimarket.handlers.bus_binding.select import positional_opener
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.lab_work.bus import _bytes
from omnimarket.models.scm import ModelScmTerminal
from omnimarket.nodes.node_ci_checkout_effect.handlers import (
    adapter_ci_checkout_cli,
    adapter_ci_checkout_serve,
)
from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_cli import (
    load_topics,
    result_from_terminal,
)
from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_serve import (
    CiCheckoutHost,
    bus_options,
    host_group_id,
    serve_group,
)
from omnimarket.nodes.node_ci_checkout_effect.handlers.handler_ci_checkout import (
    HandlerCiCheckout,
)
from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout import (
    EnumCiCheckoutStatus,
    ModelCiCheckoutRequest,
)
from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout_deployment import (
    ModelCiCheckoutDeployment,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import require_ungrouped
from tests.nodes.node_ci_checkout_effect.planted import (
    HOST,
    deployment_document,
    plant_bare_mirror,
)

pytestmark = pytest.mark.unit
OTHER_HOST = "another-host"


def _handler(mirrors: Path) -> HandlerCiCheckout:
    deployment = ModelCiCheckoutDeployment.model_validate(deployment_document(mirrors))
    return HandlerCiCheckout(overlay=deployment.resolve(HOST, home="/unused"))


def _request(sha: str, host: str = HOST) -> ModelCiCheckoutRequest:
    return ModelCiCheckoutRequest(
        correlation_id=uuid.uuid4(),
        repo="OmniNode-ai/omnimarket",
        sha=sha,
        ref=None,
        requested_by_lane="test-lane",
        host=host,
    )


async def _ask(
    bus: ProtocolLabRunBus, request: ModelCiCheckoutRequest, timeout_s: float = 10
) -> ModelScmTerminal:
    return await ScmBusCaller(bus).call_topics(
        topics=load_topics(),
        request=request,
        correlation_id=request.correlation_id,
        timeout_s=timeout_s,
    )


@pytest.mark.asyncio
async def test_golden_chain_a_held_sha_is_served_on_the_completed_topic(
    tmp_path: Path,
) -> None:
    mirrors, sha = plant_bare_mirror(tmp_path)
    async with positional_opener(open_lab_run_bus)("inmemory", None, None, None) as bus:
        host = CiCheckoutHost(bus, _handler(mirrors), HOST)
        await host.start()
        request = _request(sha)
        terminal = await _ask(bus, request)
        await host.stop()
    result = result_from_terminal(terminal.payload, request)
    assert not terminal.failed
    assert terminal.receipt.terminal_topic == load_topics().success
    assert result.status is EnumCiCheckoutStatus.SERVED
    assert result.upstream_fetches == 0
    assert result.serve_url is not None
    assert result.serve_url.startswith("file://")
    assert result.host == HOST


@pytest.mark.asyncio
async def test_error_chain_a_sha_no_source_holds_is_answered_not_found(
    tmp_path: Path,
) -> None:
    mirrors, _sha = plant_bare_mirror(tmp_path)
    async with positional_opener(open_lab_run_bus)("inmemory", None, None, None) as bus:
        host = CiCheckoutHost(bus, _handler(mirrors), HOST)
        await host.start()
        request = _request("b" * 40)
        terminal = await _ask(bus, request)
        await host.stop()
    result = result_from_terminal(terminal.payload, request)
    assert terminal.receipt.terminal_topic == load_topics().success
    assert result.status is EnumCiCheckoutStatus.NOT_FOUND
    assert result.serve_url is None


@pytest.mark.asyncio
async def test_error_chain_an_unreadable_request_ends_on_the_failed_topic(
    tmp_path: Path,
) -> None:
    mirrors, _sha = plant_bare_mirror(tmp_path)
    topics = load_topics()
    failed_topic = topics.failure
    assert failed_topic is not None
    async with positional_opener(open_lab_run_bus)("inmemory", None, None, None) as bus:
        host = CiCheckoutHost(bus, _handler(mirrors), HOST)
        await host.start()
        command = ModelEventEnvelope[dict[str, object]](
            payload={"host": HOST, "junk": 1},
            correlation_id=uuid.uuid4(),
            event_type=event_type_for(topics.command),
        )

        failures: list[bytes] = []

        async def on_failed(message: ProtocolBusMessage) -> None:
            failures.append(message.value)

        unsubscribe = await require_ungrouped(bus).subscribe_ungrouped(
            failed_topic, on_failed
        )
        await bus.publish(topics.command, b"k", _bytes(command))
        for _ in range(50):
            if failures:
                break
            await asyncio.sleep(0.1)
        await unsubscribe()
        await host.stop()
    assert failures, "no terminal on the failed topic"
    assert b'"ok": false' in failures[0]
    assert str(command.envelope_id).encode() in failures[0]


@pytest.mark.asyncio
async def test_a_request_for_another_host_is_ignored(tmp_path: Path) -> None:
    """Failing control: the host filter. The same sha asked of this host is answered; asked of another host, nothing is."""
    mirrors, sha = plant_bare_mirror(tmp_path)
    async with positional_opener(open_lab_run_bus)("inmemory", None, None, None) as bus:
        host = CiCheckoutHost(bus, _handler(mirrors), HOST)
        await host.start()
        with pytest.raises(TimeoutError):
            await _ask(bus, _request(sha, host=OTHER_HOST), timeout_s=1)
        answered = await _ask(bus, _request(sha))
        await host.stop()
    assert answered.receipt.terminal_topic == load_topics().success


def test_the_consumer_group_names_the_host_and_the_acl_prefix() -> None:
    group = host_group_id("h051")
    assert group.endswith(".h051")
    # The broker rows are prefixed on this family; a renamed service would leave the serve with no group row.
    assert group.startswith("local.omnimarket.node_ci_checkout_effect.consume.v1.")


def test_the_bus_options_come_from_the_overlay_profile_and_the_named_variable() -> None:
    document = deployment_document(Path("/unused"))
    document["profiles"]["dev"] = {
        "kind": "kafka",
        "lane": "dev",
        "workspace_root_env": "TEST_WORKSPACE_ROOT",
    }
    document["default_profile"] = "dev"
    deployment = ModelCiCheckoutDeployment.model_validate(document)
    opts = bus_options(deployment, None, {"TEST_WORKSPACE_ROOT": "/workspace"})
    assert opts == {
        "bus": "kafka",
        "bus_lane": "dev",
        "kafka_bootstrap": None,
        "omnibase_path": Path("/workspace"),
    }
    assert bus_options(deployment, "inmemory", {})["bus"] == "inmemory"


COMMAND_TOPIC_LITERAL = "onex.cmd.omnimarket.ci-checkout-requested.v1"


class _InjectedStop(asyncio.Event):
    """The serve's stop event, recorded as it is made so the test sets it instead of signalling a process."""

    made: list[_InjectedStop] = []

    def __init__(self) -> None:
        super().__init__()
        _InjectedStop.made.append(self)


class _RecordingBus:
    """An open bus that records each subscription, then sets the serve's own stop event (what SIGTERM sets)."""

    def __init__(self, bus: ProtocolLabRunBus) -> None:
        self._bus = bus
        self.subscribed: list[str] = []

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        await self._bus.publish(topic, key, value)

    async def subscribe(self, topic: str, on_message: Any, group_id: str) -> Any:
        unsubscribe = await self._bus.subscribe(
            topic, on_message=on_message, group_id=group_id
        )
        self.subscribed.append(topic)
        assert len(_InjectedStop.made) == 1, (
            "serve made no stop event before it subscribed"
        )
        _InjectedStop.made[0].set()
        return unsubscribe


def _run_serve_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, list[str]]:
    """Run ``ci-checkout serve`` through its click entry point on an in-memory bus; return the exit code and subscribed topics."""
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump(deployment_document(tmp_path / "mirrors")))
    recorders: list[_RecordingBus] = []

    @contextlib.asynccontextmanager
    async def open_bus(_opts: dict[str, object]) -> AsyncIterator[_RecordingBus]:
        async with positional_opener(open_lab_run_bus)(
            "inmemory", None, None, None
        ) as bus:
            recorder = _RecordingBus(bus)
            recorders.append(recorder)
            yield recorder

    monkeypatch.setattr(adapter_ci_checkout_serve, "_open_bus", open_bus)

    def refuse_signal(pid: int, sig: int) -> None:
        raise AssertionError(f"the test sent signal {sig} to process {pid}")

    # A signal sent to the pytest process kills the whole run when it lands before serve installs its
    # handler; the serve is stopped through its own stop event instead.
    monkeypatch.setattr(os, "kill", refuse_signal)
    _InjectedStop.made.clear()
    # Only the serve module's own name for asyncio sees the recording Event; asyncio itself is untouched.
    monkeypatch.setattr(
        adapter_ci_checkout_serve,
        "asyncio",
        SimpleNamespace(**{**vars(asyncio), "Event": _InjectedStop}),
    )
    result = CliRunner().invoke(
        serve_group,
        ["serve", "--overlay", str(overlay), "--host-name", HOST],
        catch_exceptions=False,
    )
    assert len(recorders) == 1, "the serve entry point opened no bus"
    return result.exit_code, recorders[0].subscribed


def test_ci_checkout_serve_entry_point_subscribes_the_contract_command_topic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ci-checkout serve`` listens on runtime_dispatch.command_topic, now the only place the topic is declared."""
    contract = yaml.safe_load(
        (
            Path(adapter_ci_checkout_cli.__file__).resolve().parents[1]
            / "contract.yaml"
        ).read_text()
    )
    assert contract["event_bus"]["subscribe_topics"] == []
    exit_code, subscribed = _run_serve_entry_point(tmp_path, monkeypatch)
    assert exit_code == 0
    assert subscribed == [contract["runtime_dispatch"]["command_topic"]]
    assert subscribed == [COMMAND_TOPIC_LITERAL]


def test_ci_checkout_serve_entry_point_follows_the_command_topic_of_the_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing control for a hard-coded topic: change runtime_dispatch.command_topic and the serve follows it."""
    package_dir = Path(adapter_ci_checkout_cli.__file__).resolve().parents[1]
    contract = yaml.safe_load((package_dir / "contract.yaml").read_text())
    contract["runtime_dispatch"]["command_topic"] = (
        "onex.cmd.omnimarket.ci-checkout-requested-probe.v1"
    )
    (tmp_path / "package").mkdir()
    (tmp_path / "package" / "contract.yaml").write_text(yaml.safe_dump(contract))
    monkeypatch.setattr(
        adapter_ci_checkout_cli,
        "resources",
        SimpleNamespace(files=lambda _package: tmp_path / "package"),
    )
    exit_code, subscribed = _run_serve_entry_point(tmp_path, monkeypatch)
    assert exit_code == 0
    assert subscribed == ["onex.cmd.omnimarket.ci-checkout-requested-probe.v1"]
    assert COMMAND_TOPIC_LITERAL not in subscribed
