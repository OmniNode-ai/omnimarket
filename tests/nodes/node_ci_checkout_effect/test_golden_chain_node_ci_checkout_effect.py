# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of node_ci_checkout_effect over the bus."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    EnumContractAttachExclusionReason,
    EnumWiringOutcome,
    ModelContractAttachExclusion,
    subscribe_wired_contract_topics,
    wire_from_manifest,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    ProtocolLabRunBus,
)
from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
from omnimarket.handlers.bus_binding.select import positional_opener
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.lab_work.bus import _subscribe
from omnimarket.models.scm import ModelScmTerminal
from omnimarket.nodes.node_ci_checkout_effect.handlers import handler_ci_checkout as hch
from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_cli import (
    load_topics,
    result_from_terminal,
)
from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_serve import (
    CiCheckoutHost,
)
from omnimarket.nodes.node_ci_checkout_effect.handlers.handler_ci_checkout import (
    HandlerCiCheckout,
)
from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout import (
    EnumCiCheckoutStatus,
    ModelCiCheckoutRequest,
    ModelCiCheckoutResult,
)
from tests.nodes.node_ci_checkout_effect.planted import HOST, deployment_document

pytestmark = pytest.mark.unit
BOOTSTRAP_ENV = "CI_CHECKOUT_REDPANDA_BOOTSTRAP"
# A broker that takes SASL (the lab dev lane) is opened by its lane declaration, which carries the credentials;
# the declaration is read under the workspace root in OMNIBASE_PATH.
LANE_ENV = "CI_CHECKOUT_REDPANDA_LANE"
NODE = "node_ci_checkout_effect"


def test_the_contract_names_the_handler_topics_and_overlay() -> None:
    node = resources.files(f"omnimarket.nodes.{NODE}")
    contract = yaml.safe_load(node.joinpath("contract.yaml").read_text())
    topics = load_topics()
    assert topics.command == "onex.cmd.omnimarket.ci-checkout-requested.v1"
    assert topics.success == "onex.evt.omnimarket.ci-checkout-completed.v1"
    assert topics.failure == "onex.evt.omnimarket.ci-checkout-failed.v1"
    assert contract["handler"]["class"] == "HandlerCiCheckout"
    example = yaml.safe_load(node.joinpath("overlay.example.yaml").read_text())
    assert "hosts" in example
    assert "sources" in example
    assert "unit" in example


def _git(*argv: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *argv],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


def _plant_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_overlay: bool = True
) -> str:
    """A bare mirror holding one real commit, and the overlay naming it. Returns that sha."""
    work = tmp_path / "work"
    work.mkdir(parents=True)
    _git("init", "-q", cwd=work)
    (work / "hello.txt").write_text("hello\n")
    _git("add", "hello.txt", cwd=work)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "-m",
        "one",
        cwd=work,
    )
    sha = _git("rev-parse", "HEAD", cwd=work)
    mirrors = tmp_path / "mirrors"
    mirrors.mkdir()
    _git(
        "clone",
        "-q",
        "--bare",
        str(work),
        str(mirrors / "omnimarket.git"),
        cwd=tmp_path,
    )
    if with_overlay:
        overlay_file = tmp_path / "ci-checkout-overlay.yaml"
        overlay_file.write_text(yaml.safe_dump(deployment_document(mirrors)))
        monkeypatch.setenv(hch.OVERLAY_FILE_ENV, str(overlay_file))
        monkeypatch.setenv(hch.HOST_NAME_ENV, HOST)
    else:
        monkeypatch.delenv(hch.OVERLAY_FILE_ENV, raising=False)
        monkeypatch.setenv(hch.HOST_NAME_ENV, HOST)
        monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "no-overlays"))
    return sha


@dataclass(frozen=True)
class _Wired:
    """What the shared runtime's auto-wiring did with one contract, read from its own report."""

    contract_names: tuple[str, ...]
    outcomes: dict[str, EnumWiringOutcome]
    attached: dict[str, tuple[str, ...]]
    exclusions: list[ModelContractAttachExclusion]


@contextlib.asynccontextmanager
async def _wired(
    bus_args: tuple[str, str | None, str | None, Path | None], contract_path: Path
) -> AsyncIterator[tuple[ProtocolLabRunBus, _Wired]]:
    """Wire one contract the way the runtime kernel does, on an open bus, and yield what it did."""
    manifest = discover_contracts_from_paths([contract_path])
    assert not manifest.errors, manifest.errors
    async with positional_opener(open_lab_run_bus)(*bus_args) as bus:
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            manifest,
            engine,
            event_bus=bus,
            environment="local",
            subscribe_immediately=False,
        )
        engine.freeze()
        exclusions: list[ModelContractAttachExclusion] = []
        attached = await subscribe_wired_contract_topics(
            manifest,
            report,
            engine,
            bus,
            environment="local",
            exclusions_out=exclusions,
        )
        yield (
            bus,
            _Wired(
                contract_names=tuple(c.name for c in manifest.contracts),
                outcomes={r.contract_name: r.outcome for r in report.results},
                attached=dict(attached),
                exclusions=exclusions,
            ),
        )


async def _assert_shared_runtime_wires_nothing(
    bus: ProtocolLabRunBus, wired: _Wired
) -> None:
    """The node is loaded, the runtime skipped it for want of subscribe_topics, and nothing listens.

    Each clause fails on its own when the node is never discovered, when the manifest key changes, or
    when a non-empty subscribe_topics makes the runtime attach a consumer; a bare "nothing attached"
    would pass in all three.
    """
    command = load_topics().command
    assert wired.contract_names == (NODE,), wired.contract_names
    assert wired.outcomes.get(NODE) is EnumWiringOutcome.SKIPPED, wired.outcomes
    skipped = [x for x in wired.exclusions if x.contract_name == NODE]
    assert len(skipped) == 1, wired.exclusions
    assert skipped[0].reason is EnumContractAttachExclusionReason.NOT_WIRED
    assert "No event_bus.subscribe_topics declared" in skipped[0].detail
    assert not wired.attached.get(NODE)
    assert await _consumers_on(bus, command) == 0


async def _consumers_on(bus: ProtocolLabRunBus, topic: str) -> int:
    """How many consumers this bus client holds on ``topic``, read the way each bus exposes it.

    The in-memory bus counts subscribers (``get_subscriber_count``); the Kafka bus has no such call and
    exposes its live group consumers keyed by (topic, group) through ``get_consumer_groups``. Neither is on
    ProtocolLabRunBus, so a bus with neither read fails here instead of passing as "no consumer".
    """
    count_subscribers = getattr(bus, "get_subscriber_count", None)
    if count_subscribers is not None:
        return int(await count_subscribers(topic))
    consumer_groups = getattr(bus, "get_consumer_groups", None)
    assert consumer_groups is not None, f"{type(bus).__name__} exposes no consumer read"
    return sum(1 for (consumed, _group) in consumer_groups() if consumed == topic)


class _TerminalCollector:
    """Every message on both terminal topics, so a second terminal for one request is seen, not lost."""

    def __init__(self, bus: ProtocolLabRunBus) -> None:
        self._bus = bus
        self._unsubscribers: list[Any] = []
        self.seen: list[tuple[str, dict[str, Any]]] = []

    async def start(self) -> None:
        topics = load_topics()
        group = f"ci-checkout-golden-collector-{uuid.uuid4()}"
        for topic in (topics.success, topics.failure):
            assert topic is not None

            async def on_message(
                message: ProtocolBusMessage, topic: str = topic
            ) -> None:
                self.seen.append((topic, json.loads(message.value)))

            self._unsubscribers.append(
                await _subscribe(self._bus, topic, on_message, group, "latest")
            )

    async def stop(self) -> None:
        for unsubscribe in self._unsubscribers:
            await unsubscribe()
        self._unsubscribers.clear()

    def topics_for(self, correlation_id: uuid.UUID) -> list[str]:
        return [
            topic
            for topic, raw in self.seen
            if raw.get("correlation_id") == str(correlation_id)
        ]


async def _chain(
    bus_args: tuple[str, str | None, str | None, Path | None],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sha_held: bool,
    with_overlay: bool = True,
) -> tuple[ModelCiCheckoutResult, ModelScmTerminal, list[str]]:
    """One request through the per-host serve, with the shared runtime's wiring proven empty first.

    Returns the result, the terminal the caller read, and the topic of every terminal published for
    the request's correlation id (so a failed terminal racing the real answer is a second entry).
    """
    held_sha = _plant_deployment(tmp_path, monkeypatch, with_overlay=with_overlay)
    node = resources.files(f"omnimarket.nodes.{NODE}")
    with resources.as_file(node.joinpath("contract.yaml")) as path:
        # ONEX_CI_CHECKOUT_HOST_NAME is set here (see _plant_deployment): with it set the runtime
        # still must not answer through auto-wiring, the path the handler's own error text names.
        async with _wired(bus_args, path) as (bus, wired):
            await _assert_shared_runtime_wires_nothing(bus, wired)
            collector = _TerminalCollector(bus)
            await collector.start()
            host = CiCheckoutHost(bus, HandlerCiCheckout(), HOST)
            await host.start()
            request = ModelCiCheckoutRequest(
                correlation_id=uuid.uuid4(),
                repo="OmniNode-ai/omnimarket",
                sha=held_sha if sha_held else "b" * 40,
                ref=None,
                requested_by_lane="test-lane",
                host=HOST,
            )
            try:
                terminal = await ScmBusCaller(bus).call_topics(
                    topics=load_topics(),
                    request=request,
                    correlation_id=request.correlation_id,
                    timeout_s=20,
                )
            finally:
                # stop() drains every answer task, so a late second terminal is on the bus after it.
                await host.stop()
                await asyncio.sleep(0)
                await collector.stop()
    assert terminal.receipt.terminal_envelope_id is not None
    terminal_topics = collector.topics_for(request.correlation_id)
    return result_from_terminal(terminal.payload, request), terminal, terminal_topics


@pytest.mark.asyncio
async def test_ci_checkout_golden_chain_on_the_in_memory_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, terminal, published = await _chain(
        ("inmemory", None, None, None), tmp_path, monkeypatch, sha_held=True
    )
    assert result.status is EnumCiCheckoutStatus.SERVED, result
    assert result.upstream_fetches == 0
    assert result.serve_url is not None
    assert result.serve_url.startswith("file://")
    assert not terminal.failed
    assert terminal.receipt.terminal_topic == load_topics().success
    # The reported race: a failed terminal beside the real answer. Exactly one terminal, on success.
    assert published == [load_topics().success], published
    assert load_topics().failure not in published


@pytest.mark.asyncio
async def test_ci_checkout_golden_error_chain_a_sha_no_source_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _terminal, published = await _chain(
        ("inmemory", None, None, None), tmp_path, monkeypatch, sha_held=False
    )
    assert result.status is EnumCiCheckoutStatus.NOT_FOUND, result
    assert result.serve_url is None
    # Failing control: the golden chain's assertion on the same node reads SERVED, so this
    # answer differs from it only by the sha, which is what the node reads.
    assert result.status is not EnumCiCheckoutStatus.SERVED
    # A sha no source holds is an answer, not a failure: one completed terminal, none failed.
    assert published == [load_topics().success], published


@pytest.mark.asyncio
async def test_ci_checkout_golden_error_chain_no_overlay_ends_on_the_failure_topic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, terminal, published = await _chain(
        ("inmemory", None, None, None),
        tmp_path,
        monkeypatch,
        sha_held=True,
        with_overlay=False,
    )
    assert terminal.failed
    assert terminal.receipt.terminal_topic == load_topics().failure
    assert result.status is EnumCiCheckoutStatus.FAILED
    assert result.detail
    assert published == [load_topics().failure], published


@pytest.mark.asyncio
async def test_ci_checkout_golden_collector_sees_a_failed_terminal_racing_the_answer() -> (
    None
):
    """Failing control for the terminal count: a failed terminal beside the real answer is seen, not lost.

    ScmBusCaller returns the first terminal, so the race (a failed terminal beside the answer) is invisible
    to a test that reads only its return value. The collector reads both topics.
    """
    topics = load_topics()
    correlation_id = uuid.uuid4()
    async with positional_opener(open_lab_run_bus)("inmemory", None, None, None) as bus:
        collector = _TerminalCollector(bus)
        await collector.start()
        try:
            for topic in (topics.success, topics.failure):
                assert topic is not None
                body = {"correlation_id": str(correlation_id), "payload": {}}
                await bus.publish(topic, b"k", json.dumps(body).encode())
            await bus.publish(
                topics.success,
                b"k",
                json.dumps({"correlation_id": str(uuid.uuid4())}).encode(),
            )
        finally:
            await collector.stop()
    assert sorted(collector.topics_for(correlation_id)) == sorted(
        [topics.success, topics.failure]
    )


def _count_handlers_built(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record every HandlerCiCheckout built from here on."""
    built: list[object] = []
    original = HandlerCiCheckout.__init__

    def counting_init(self: HandlerCiCheckout, *args: Any, **kwargs: Any) -> None:
        built.append(self)
        original(self, *args, **kwargs)

    monkeypatch.setattr(HandlerCiCheckout, "__init__", counting_init)
    return built


@pytest.mark.asyncio
async def test_ci_checkout_golden_control_a_non_empty_subscribe_topics_attaches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for the negative assertion: the same contract with the command topic subscribed attaches.

    This is the contract the dev runtime auto-wired before the change. If the control did not attach, the
    "wires nothing" assertion above could not tell a contract that is empty from a runtime that never looks.
    """
    monkeypatch.setenv(hch.HOST_NAME_ENV, HOST)
    built = _count_handlers_built(monkeypatch)
    node = resources.files(f"omnimarket.nodes.{NODE}")
    contract = yaml.safe_load(node.joinpath("contract.yaml").read_text())
    command = contract["runtime_dispatch"]["command_topic"]
    contract["event_bus"]["subscribe_topics"] = [command]
    wired_contract = tmp_path / NODE / "contract.yaml"
    wired_contract.parent.mkdir()
    wired_contract.write_text(yaml.safe_dump(contract))
    async with _wired(("inmemory", None, None, None), wired_contract) as (bus, wired):
        assert wired.contract_names == (NODE,)
        assert wired.outcomes.get(NODE) is EnumWiringOutcome.WIRED, wired.outcomes
        assert wired.attached.get(NODE) == (command,), wired.attached
        assert await _consumers_on(bus, command) == 1
        with pytest.raises(AssertionError):
            await _assert_shared_runtime_wires_nothing(bus, wired)
    # Control for the "builds no handler" test below: the counter sees the runtime build the handler.
    assert built, (
        "the runtime built no HandlerCiCheckout from a contract that subscribes"
    )


@pytest.mark.asyncio
async def test_ci_checkout_golden_shared_runtime_builds_no_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the host name set the runtime still builds no HandlerCiCheckout from this contract."""
    monkeypatch.setenv(hch.HOST_NAME_ENV, HOST)
    built = _count_handlers_built(monkeypatch)
    node = resources.files(f"omnimarket.nodes.{NODE}")
    with resources.as_file(node.joinpath("contract.yaml")) as path:
        async with _wired(("inmemory", None, None, None), path) as (bus, wired):
            await _assert_shared_runtime_wires_nothing(bus, wired)
    assert built == []


@pytest.mark.kafka
@pytest.mark.integration
@pytest.mark.asyncio
async def test_ci_checkout_golden_and_error_chains_on_a_redpanda_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap = os.environ.get(BOOTSTRAP_ENV)
    if not bootstrap:
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    lane = os.environ.get(LANE_ENV)
    bus_args: tuple[str, str | None, str | None, Path | None] = (
        ("kafka", lane, None, Path(os.environ["OMNIBASE_PATH"]))
        if lane
        else ("kafka", None, bootstrap, None)
    )
    golden, golden_terminal, golden_published = await _chain(
        bus_args, tmp_path / "g", monkeypatch, sha_held=True
    )
    error, error_terminal, error_published = await _chain(
        bus_args, tmp_path / "e", monkeypatch, sha_held=False
    )
    assert golden.status is EnumCiCheckoutStatus.SERVED
    assert error.status is EnumCiCheckoutStatus.NOT_FOUND
    # The bus with real consumer groups: any other consumer of the command topic on this broker (a
    # shared runtime that auto-wired the node) would put a second terminal, on the failure topic, here.
    success = load_topics().success
    assert not golden_terminal.failed
    assert not error_terminal.failed
    assert golden_published == [success], golden_published
    assert error_published == [success], error_published


@pytest.mark.asyncio
async def test_ci_checkout_golden_consumer_read_works_on_the_kafka_bus() -> None:
    """The redpanda chain reads its consumers through the same helper; the Kafka bus must answer it, not raise."""
    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka

    bus = EventBusKafka.from_bootstrap("127.0.0.1:1")
    assert not hasattr(bus, "get_subscriber_count")
    assert await _consumers_on(bus, load_topics().command) == 0  # type: ignore[arg-type]
