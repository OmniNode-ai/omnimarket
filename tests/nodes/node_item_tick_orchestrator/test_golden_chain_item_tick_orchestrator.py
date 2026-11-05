# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the durable item tick and its correlated cursor protocol."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_item_tick_orchestrator.handlers.handler_item_tick import (
    HandlerItemTick,
    liveness_overlay,
)
from omnimarket.nodes.node_item_tick_orchestrator.models.model_item_tick import (
    ModelItemTickConfig,
    ModelItemTickRequest,
)

CHECKS = ("item-tick", "S5", "S6", "S7", "S8", "S9", "goal")


def configuration(tmp_path: Path) -> ModelItemTickConfig:
    bindings = {}
    for name in CHECKS:
        contract = tmp_path / f"{name}.yaml"
        contract.write_text(
            yaml.safe_dump(
                {
                    "runtime_dispatch": {
                        "command_topic": f"onex.cmd.omnimarket.test-{name.lower()}.v1"
                    },
                    "terminal_event": f"onex.evt.omnimarket.test-{name.lower()}-completed.v1",
                }
            )
        )
        bindings[name] = {"contract_path": str(contract), "request": {"check": name}}
    return ModelItemTickConfig(
        schema_version="item-tick/v1",
        bus="inmemory",
        lane=None,
        state_directory=tmp_path / "state",
        host="test-host",
        interval_seconds=300,
        check_timeout_seconds=0.1,
        checks=bindings,
    )


@pytest.mark.asyncio
async def test_tick_unit_bus_runs_all_checks_and_persists_each_cursor(
    tmp_path: Path,
) -> None:
    config = configuration(tmp_path)
    seen = []

    async def call(binding, correlation_id):
        seen.append(binding.request["check"])
        return {"status": "success", "lines": ["open=1 oldest=12m"]}

    result = await HandlerItemTick(None, call=call).handle(
        ModelItemTickRequest(
            config=config, now=datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
        )
    )
    assert result.status == "success"
    assert tuple(seen) == CHECKS
    assert len(result.cursors) == len(CHECKS)
    assert "open=1 oldest=12m" in result.lines
    cursors = [
        json.loads(line)
        for line in (config.state_directory / "cursors.jsonl").read_text().splitlines()
    ]
    assert {row["check"] for row in cursors} == set(CHECKS)


@pytest.mark.asyncio
async def test_tick_unit_a_missing_check_does_not_refresh_its_cursor_or_stop_the_others(
    tmp_path: Path,
) -> None:
    config = configuration(tmp_path)

    async def call(binding, correlation_id):
        if binding.request["check"] == "S6":
            raise TimeoutError("missing completion")
        return {"status": "success"}

    result = await HandlerItemTick(None, call=call).handle(
        ModelItemTickRequest(config=config)
    )
    assert result.status == "failure"
    assert "S6" in result.errors
    assert {row.check for row in result.cursors} == set(CHECKS) - {"S6"}


@pytest.mark.asyncio
async def test_tick_unit_refuses_an_incomplete_overlay_before_running_any_check(
    tmp_path: Path,
) -> None:
    config = configuration(tmp_path)
    config = config.model_copy(update={"checks": {"S5": config.checks["S5"]}})
    seen = []

    async def call(binding, correlation_id):
        seen.append(correlation_id)
        return {"status": "success"}

    with pytest.raises(ValueError, match="CHECK-BINDINGS"):
        await HandlerItemTick(None, call=call).handle(
            ModelItemTickRequest(config=config)
        )
    assert seen == []


@pytest.mark.asyncio
async def test_tick_unit_real_inmemory_bus_uses_contract_topics_and_correlated_terminals(
    tmp_path: Path,
) -> None:
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

    config = configuration(tmp_path)
    bus = EventBusInmemory(environment="local", group="item-tick-test")
    await bus.start()
    try:
        for binding in config.checks.values():
            contract = yaml.safe_load(binding.contract_path.read_text())
            command, terminal = (
                contract["runtime_dispatch"]["command_topic"],
                contract["terminal_event"],
            )

            async def respond(message, topic=terminal):
                incoming = json.loads(message.value)
                envelope = ModelEventEnvelope[object](
                    payload={"status": "success"},
                    correlation_id=incoming["correlation_id"],
                    parent_envelope_id=incoming["envelope_id"],
                    event_type="omnimarket.test-completed",
                )
                await bus.publish(
                    topic, None, envelope.model_dump_json().encode(), None
                )

            await bus.subscribe(command, group_id=f"test-{uuid4()}", on_message=respond)
        result = await HandlerItemTick(event_bus=bus).handle(
            ModelItemTickRequest(config=config)
        )
        assert result.status == "success", result.errors
        assert {row.check for row in result.cursors} == set(CHECKS)
    finally:
        await bus.close()


def test_tick_unit_watchdog_reports_the_missing_start_and_a_stale_check_cursor(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_automation_liveness_watchdog_compute.handlers.watchdog_verdicts import (
        evaluate_process,
    )
    from omnimarket.nodes.node_automation_liveness_watchdog_compute.models.model_automation_liveness_watchdog import (
        ModelProcessRunFacts,
    )

    config = configuration(tmp_path)
    overlay = liveness_overlay(config)
    entries = {entry.process_id: entry for entry in overlay.processes}
    now = datetime(2026, 11, 8, 12, tzinfo=UTC)
    timer = entries["item-tick/timer"]
    stopped = ModelProcessRunFacts(
        process_id=timer.process_id,
        host=config.host,
        declared_at=now - timedelta(hours=1),
        last_run_at=now - timedelta(seconds=301),
    )
    assert evaluate_process(timer, stopped, now).verdict.value == "missed"
    for name in CHECKS:
        entry = entries[f"item-tick/{name.lower()}"]
        age = 601 if name == "S6" else 30
        facts = ModelProcessRunFacts(
            process_id=entry.process_id,
            host=config.host,
            declared_at=now - timedelta(hours=1),
            last_run_at=now - timedelta(seconds=age),
        )
        expected = "missed" if name == "S6" else "healthy"
        assert evaluate_process(entry, facts, now).verdict.value == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("overlay_name", ["inmemory", "redpanda"])
async def test_tick_unit_timer_ingress_wires_the_registered_node_on_the_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overlay_name: str
) -> None:
    from contextlib import asynccontextmanager
    from functools import wraps

    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

    from omnimarket.nodes.node_item_tick_orchestrator import (
        runtime_item_tick as ingress,
    )

    config = configuration(tmp_path)
    overlay = yaml.safe_load(
        (ingress.CONTRACT.parent / "overlays" / f"{overlay_name}.yaml").read_text()
    )
    config = ModelItemTickConfig.model_validate(
        {**config.model_dump(), "bus": overlay["bus"], "lane": overlay["lane"]}
    )
    bus = EventBusInmemory(environment="local", group="item-tick-ingress")
    await bus.start()
    observed = {}

    async def record(message):
        observed.setdefault(message.topic, []).append(json.loads(message.value))

    for topic in (
        "onex.evt.omnimarket.item-tick-completed.v1",
        "onex.evt.omnimarket.automation-run-observed.v1",
    ):
        await bus.subscribe(topic, group_id=f"observe-{uuid4()}", on_message=record)

    @asynccontextmanager
    @wraps(ingress.open_lab_run_bus)
    async def open_bus(**kwargs):
        assert kwargs["bus"] == overlay["bus"]
        assert kwargs["lane"] == overlay["lane"]
        yield bus

    monkeypatch.setattr(ingress, "open_lab_run_bus", open_bus)
    try:
        if overlay_name == "redpanda":
            # The deployed side is represented by a separately wired runtime on
            # the test bus. The timer must not host its own second consumer.
            from omnibase_infra.runtime.auto_wiring.discovery import (
                discover_contracts_from_paths,
            )
            from omnibase_infra.runtime.auto_wiring.handler_wiring import (
                wire_from_manifest,
            )
            from omnibase_infra.runtime.message_dispatch_engine import (
                MessageDispatchEngine,
            )

            manifest = discover_contracts_from_paths([ingress.CONTRACT])
            engine = MessageDispatchEngine()
            report = await wire_from_manifest(
                manifest, engine, event_bus=bus, environment="local"
            )
            assert all(result.outcome.value == "wired" for result in report.results)
            engine.freeze()

            async def refuse_client_hosting(*args, **kwargs):
                raise AssertionError("the Redpanda timer must not host handlers")

            monkeypatch.setattr(ingress, "wire_from_manifest", refuse_client_hosting)
        for binding in config.checks.values():
            contract = yaml.safe_load(binding.contract_path.read_text())

            async def respond(message, topic=contract["terminal_event"]):
                incoming = json.loads(message.value)
                envelope = ModelEventEnvelope[object](
                    payload={"status": "success"},
                    correlation_id=incoming["correlation_id"],
                    parent_envelope_id=incoming["envelope_id"],
                    event_type="omnimarket.test-completed",
                )
                await bus.publish(
                    topic, None, envelope.model_dump_json().encode(), None
                )

            await bus.subscribe(
                contract["runtime_dispatch"]["command_topic"],
                group_id=f"test-{uuid4()}",
                on_message=respond,
            )
        result = await ingress.dispatch(config, None)
        assert result.status == "success", result.errors
        assert len(result.cursors) == len(CHECKS)
        terminals = observed["onex.evt.omnimarket.item-tick-completed.v1"]
        assert len(terminals) == 1
        assert terminals[0]["payload"]["status"] == "success"
        assert terminals[0]["payload"]["fire_id"] == result.fire_id
        runs = observed["onex.evt.omnimarket.automation-run-observed.v1"]
        assert len(runs) == 2 * (len(CHECKS) + 1)
        assert {row["payload"]["phase"] for row in runs} == {"started", "finished"}
        assert {row["payload"]["process_id"] for row in runs} == {
            f"item-tick/{name.lower()}" for name in ("timer", *CHECKS)
        }
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_tick_unit_concurrent_start_returns_locked_without_running_a_check(
    tmp_path: Path,
) -> None:
    import fcntl

    config = configuration(tmp_path)
    config.state_directory.mkdir()
    seen = []

    async def call(binding, correlation_id):
        seen.append(correlation_id)
        return {"status": "success"}

    with (config.state_directory / "tick.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = await HandlerItemTick(None, call=call).handle(
            ModelItemTickRequest(config=config)
        )
    assert result.status == "locked"
    assert result.lines == ("LOCKED",)
    assert seen == []
    assert not (config.state_directory / "cursors.jsonl").exists()


@pytest.mark.asyncio
async def test_tick_unit_cursors_survive_a_new_handler_and_a_new_interval(
    tmp_path: Path,
) -> None:
    config = configuration(tmp_path)

    async def call(binding, correlation_id):
        return {"status": "success"}

    now = datetime(2026, 11, 8, 12, tzinfo=UTC)
    first = await HandlerItemTick(None, call=call).handle(
        ModelItemTickRequest(config=config, now=now)
    )
    second = await HandlerItemTick(None, call=call).handle(
        ModelItemTickRequest(config=config, now=now + timedelta(minutes=5))
    )
    assert first.status == second.status == "success"
    rows = [
        json.loads(line)
        for line in (config.state_directory / "cursors.jsonl").read_text().splitlines()
    ]
    assert [row["check"] for row in rows] == list(CHECKS) * 2
    assert {row["fire_id"] for row in rows} == {first.fire_id, second.fire_id}
    assert (
        json.loads((config.state_directory / "last-run.json").read_text())["fire_id"]
        == second.fire_id
    )


def test_tick_unit_installer_configuration_probe_has_a_positive_control(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from omnimarket.nodes.node_item_tick_orchestrator import (
        runtime_item_tick as ingress,
    )

    config = configuration(tmp_path)
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    assert ingress.main(["--overlay", str(overlay), "--check-config"]) == 0
    assert "ITEM-TICK CONFIGURED" in capsys.readouterr().out
    assert not config.state_directory.exists()
    overlay.write_text((ingress.CONTRACT.parent / "overlays/redpanda.yaml").read_text())
    assert ingress.main(["--overlay", str(overlay), "--check-config"]) == 1
    assert "CHECK-BINDINGS" in capsys.readouterr().err


def test_tick_unit_node_is_declarative() -> None:
    from omnibase_core.nodes.node_orchestrator import NodeOrchestrator

    from omnimarket.nodes.node_item_tick_orchestrator.node import (
        NodeItemTickOrchestrator,
    )

    assert issubclass(NodeItemTickOrchestrator, NodeOrchestrator)
