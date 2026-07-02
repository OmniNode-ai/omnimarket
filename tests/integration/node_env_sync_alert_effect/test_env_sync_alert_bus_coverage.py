# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full I/O-boundary EFFECT coverage for node_env_sync_alert_effect, driven over
the canonical in-memory bus.

OMN-13674 (cluster side-effect-alert-render-discovery, archetype effect). A
``ModelEnvSyncAlertRequest`` lands on the declared command topic
``onex.cmd.omnimarket.env-sync-alert-start.v1`` and the terminal
``ModelEnvSyncAlertResult`` is auto-published onto the declared completed topic
``onex.evt.omnimarket.env-sync-alert-completed.v1`` by
``LocalRuntimeBusAdapter``. No live Kafka / ``.201``.

The Linear write boundary is replaced by a constructor-injected
``_MockLinearAdapter`` (the canonical ``_Mock*`` injection pattern) — subprocess
and asyncpg are never monkeypatched and no real Linear ticket is ever created,
so no prod-mutating effect is exercised. The friction-YAML write boundary is a
real filesystem write into a pytest ``tmp_path`` (never a prod surface).

Declared-state coverage (contract ``outputs`` + ``event_bus.publish_topics``):
  * ``alerts_created`` — asserted at 0 (no linear) and >0 (linear success);
  * ``friction_events`` — asserted emitted, thresholded, and empty (no drift);
  * ``onex.evt.omnimarket.env-sync-alert-completed.v1`` — the terminal topic the
    result is published onto over the bus;
  * ``onex.evt.omnimarket.friction-emitted.v1`` — declared publish topic; the
    handler emits friction as filesystem YAML rather than a bus event, so this
    test asserts the contract declaration and that no bus event lands on it (an
    honest record of the declared-but-unwired topic).

EFFECT DoD covered — every outcome at the injected/real I/O boundary:
  * success, friction emitted, no linear ticket (``create_linear_tickets`` off);
  * success, linear ticket created through the injected adapter;
  * below-threshold no-op (``alert_threshold`` above the occurrence count);
  * negative control: a known-good log with no drift lines emits nothing;
  * gate-blocked failure: ``create_linear_tickets`` on with NO adapter injected
    raises inside the handler -> the adapter swallows it and NO terminal event is
    published (empty terminal history);
  * retry/failure: an injected adapter that raises on ``create_ticket`` -> no
    terminal event;
  * idempotency: identical input yields identical drift signature, count, and
    friction path across two independent bus runs.
Typed result fields are asserted off the terminal event — never a bare
"returned without raising".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_env_sync_alert_effect.handlers.handler_env_sync_alert_effect import (
    HandlerEnvSyncAlertEffect,
)
from omnimarket.nodes.node_env_sync_alert_effect.models.model_env_sync_alert_request import (
    ModelEnvSyncAlertRequest,
)
from omnimarket.nodes.node_env_sync_alert_effect.models.model_env_sync_alert_result import (
    ModelEnvSyncAlertResult,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

TOPIC_COMMAND = "onex.cmd.omnimarket.env-sync-alert-start.v1"
TOPIC_COMPLETED = "onex.evt.omnimarket.env-sync-alert-completed.v1"
TOPIC_FRICTION = "onex.evt.omnimarket.friction-emitted.v1"

_DRIFT_LOG = "\n".join(
    [
        "ok startup",
        "ENV_SYNC_DRIFT missing DATABASE_URL on stability runtime",
        "environment sync drift: DATABASE_URL differs from contract",
    ]
)
_CLEAN_LOG = "\n".join(
    [
        "ok startup",
        "all services healthy",
        "config reconciled successfully",
    ]
)


class _MockLinearAdapter:
    """Constructor-injected Linear write seam — records payloads, never calls Linear."""

    def __init__(self, *, raise_on_create: bool = False) -> None:
        self.payloads: list[dict[str, Any]] = []
        self._raise = raise_on_create

    def create_ticket(self, payload: dict[str, Any]) -> str:
        if self._raise:
            raise RuntimeError("linear boom")
        self.payloads.append(payload)
        return f"OMN-{len(self.payloads)}"


def _write_log(tmp_path: Path, text: str, name: str = "runtime.log") -> Path:
    log_path = tmp_path / name
    log_path.write_text(text, encoding="utf-8")
    return log_path


async def _collect_terminal(
    bus: Any,
    request: ModelEnvSyncAlertRequest,
    *,
    linear_adapter: _MockLinearAdapter | None = None,
) -> list[Any]:
    """Publish the start command; return the terminal-event history (may be empty)."""
    adapter = LocalRuntimeBusAdapter(
        handler=HandlerEnvSyncAlertEffect(linear_adapter=linear_adapter),
        handler_name="env-sync-alert",
        input_model_cls=ModelEnvSyncAlertRequest,
        output_topic=TOPIC_COMPLETED,
        bus=bus,
    )
    await bus.subscribe(
        TOPIC_COMMAND,
        on_message=adapter.on_message,
        group_id="omnimarket-env-sync-test",
    )
    await bus.publish(
        TOPIC_COMMAND, key=None, value=request.model_dump_json().encode("utf-8")
    )
    history: list[Any] = list(await bus.get_event_history(topic=TOPIC_COMPLETED))
    return history


def _result_from(history: list[Any]) -> ModelEnvSyncAlertResult:
    assert len(history) == 1, f"expected exactly one terminal event, got {history}"
    assert history[-1].topic == TOPIC_COMPLETED
    return ModelEnvSyncAlertResult.model_validate(json.loads(history[-1].value))


# ---------------------------------------------------------------------------
# success: drift found, friction emitted, no linear ticket requested.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_friction_emitted_no_linear_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        log_path = _write_log(tmp_path, _DRIFT_LOG)
        friction_dir = tmp_path / "friction"
        history = await _collect_terminal(
            bus,
            ModelEnvSyncAlertRequest(
                log_paths=[str(log_path)],
                alert_threshold=1,
                friction_dir=str(friction_dir),
            ),
        )
        result = _result_from(history)
        assert result.alerts_created == 0
        assert len(result.friction_events) == 1
        event = result.friction_events[0]
        assert event["env_keys"] == ["DATABASE_URL"]
        assert event["event_type"] == "env_sync_drift"
        assert Path(event["friction_path"]).is_file()
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# success: linear ticket created through the injected adapter.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_linear_ticket_created_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        log_path = _write_log(tmp_path, "env sync drift: REDPANDA_URL missing\n")
        linear = _MockLinearAdapter()
        history = await _collect_terminal(
            bus,
            ModelEnvSyncAlertRequest(
                log_paths=[str(log_path)],
                friction_dir=str(tmp_path / "friction"),
                create_linear_tickets=True,
            ),
            linear_adapter=linear,
        )
        result = _result_from(history)
        assert result.alerts_created == 1
        assert len(result.friction_events) == 1
        assert len(linear.payloads) == 1
        assert linear.payloads[0]["drift_signature"]
        assert "REDPANDA_URL" in linear.payloads[0]["title"]
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# below-threshold no-op: occurrences under alert_threshold emit nothing.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_below_threshold_no_alert_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        # Single drift occurrence, threshold of 5 -> nothing is emitted.
        log_path = _write_log(tmp_path, "ENV_SYNC_DRIFT missing KAFKA_URL once\n")
        history = await _collect_terminal(
            bus,
            ModelEnvSyncAlertRequest(
                log_paths=[str(log_path)],
                alert_threshold=5,
                friction_dir=str(tmp_path / "friction"),
            ),
        )
        result = _result_from(history)
        assert result.alerts_created == 0
        assert result.friction_events == []
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# negative control: a clean log with no drift lines produces no findings.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_no_drift_negative_control_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        log_path = _write_log(tmp_path, _CLEAN_LOG)
        history = await _collect_terminal(
            bus,
            ModelEnvSyncAlertRequest(
                log_paths=[str(log_path)],
                alert_threshold=1,
                friction_dir=str(tmp_path / "friction"),
            ),
        )
        result = _result_from(history)
        assert result.alerts_created == 0
        assert result.friction_events == []
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# gate-blocked failure: create_linear_tickets on, no adapter injected ->
# handler raises -> adapter swallows -> NO terminal event published.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_linear_requested_without_adapter_blocks_terminal_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        log_path = _write_log(tmp_path, _DRIFT_LOG)
        history = await _collect_terminal(
            bus,
            ModelEnvSyncAlertRequest(
                log_paths=[str(log_path)],
                friction_dir=str(tmp_path / "friction"),
                create_linear_tickets=True,
            ),
            linear_adapter=None,
        )
        # RuntimeError inside the handler -> no result -> empty terminal history.
        assert history == []
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# retry/failure: injected adapter raises on create_ticket -> no terminal event.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_linear_adapter_raises_blocks_terminal_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        log_path = _write_log(tmp_path, _DRIFT_LOG)
        linear = _MockLinearAdapter(raise_on_create=True)
        history = await _collect_terminal(
            bus,
            ModelEnvSyncAlertRequest(
                log_paths=[str(log_path)],
                friction_dir=str(tmp_path / "friction"),
                create_linear_tickets=True,
            ),
            linear_adapter=linear,
        )
        assert history == []
        assert linear.payloads == []
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# idempotency: identical input yields identical drift signature / count / path.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_idempotent_identical_input_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    bus_factory = type(integration_event_bus)
    log_path = _write_log(tmp_path, _DRIFT_LOG)
    friction_dir = tmp_path / "friction"
    signatures: list[tuple[str, int, str]] = []
    for _ in range(2):
        bus = bus_factory(
            environment="integration-test", group="omnimarket-integration"
        )
        await bus.start()
        try:
            history = await _collect_terminal(
                bus,
                ModelEnvSyncAlertRequest(
                    log_paths=[str(log_path)],
                    alert_threshold=1,
                    friction_dir=str(friction_dir),
                ),
            )
            result = _result_from(history)
            event = result.friction_events[0]
            signatures.append(
                (event["drift_signature"], event["count"], event["friction_path"])
            )
        finally:
            await bus.close()
    # The drift signature, occurrence count, and target friction path are
    # deterministic across independent runs (only occurred_at, a wall clock, differs).
    assert signatures[0] == signatures[1]


# ---------------------------------------------------------------------------
# declared friction-emitted topic: honest record of the declared-but-unwired
# publish topic. The handler emits friction as filesystem YAML, not a bus event.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_friction_topic_declared_but_emitted_as_yaml_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_env_sync_alert_effect"
        / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    publish_topics = contract["event_bus"]["publish_topics"]
    assert TOPIC_FRICTION in publish_topics
    assert TOPIC_COMPLETED in publish_topics

    bus = integration_event_bus
    await bus.start()
    try:
        log_path = _write_log(tmp_path, _DRIFT_LOG)
        friction_dir = tmp_path / "friction"
        history = await _collect_terminal(
            bus,
            ModelEnvSyncAlertRequest(
                log_paths=[str(log_path)],
                alert_threshold=1,
                friction_dir=str(friction_dir),
            ),
        )
        result = _result_from(history)
        # The friction boundary is a real YAML file, not a bus event on the
        # declared friction-emitted topic.
        friction_history = await bus.get_event_history(topic=TOPIC_FRICTION)
        assert friction_history == []
        assert list(friction_dir.glob("env-sync-drift-*.yaml"))
        assert Path(result.friction_events[0]["friction_path"]).is_file()
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# friction-dir resolution branches (direct-call): the ONEX_STATE_DIR / OMNI_HOME
# / bare-default fallback ladder in _resolve_friction_dir.
# ---------------------------------------------------------------------------


def test_friction_dir_resolves_from_onex_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("OMNI_HOME", raising=False)
    log_path = _write_log(tmp_path, _DRIFT_LOG)
    result = HandlerEnvSyncAlertEffect().handle(
        ModelEnvSyncAlertRequest(log_paths=[str(log_path)], friction_dir=None)
    )
    assert len(result.friction_events) == 1
    assert (
        str(tmp_path / "state" / "friction")
        in result.friction_events[0]["friction_path"]
    )


def test_friction_dir_resolves_from_omni_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path / "home"))
    log_path = _write_log(tmp_path, _DRIFT_LOG)
    result = HandlerEnvSyncAlertEffect().handle(
        ModelEnvSyncAlertRequest(log_paths=[str(log_path)], friction_dir=None)
    )
    assert len(result.friction_events) == 1
    expected = str(tmp_path / "home" / ".onex_state" / "friction")
    assert expected in result.friction_events[0]["friction_path"]


def test_missing_log_paths_are_skipped(tmp_path: Path) -> None:
    # A non-existent path is silently skipped (path.is_file() is False) -> no findings.
    result = HandlerEnvSyncAlertEffect().handle(
        ModelEnvSyncAlertRequest(
            log_paths=[str(tmp_path / "does-not-exist.log")],
            friction_dir=str(tmp_path / "friction"),
        )
    )
    assert result.alerts_created == 0
    assert result.friction_events == []
