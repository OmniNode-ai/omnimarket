# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker-listener condition of node_lab_alarm and its seam to the alarm delivery (OMN-23933).

Golden: an answering address reads OK and raises nothing. Error: every address refused or timed out reads ALARM
with one typed alarm, delivered once through the alarm delivery node and ended by the next OK. INDETERMINATE: an
unresolvable name or a malformed bootstrap string, never read as healthy and never delivered as an alarm. Seam:
the alarm line splits into the subject the delivery keys on; the TCP probe maps each socket failure to the
typed error the handler reads (a local server answers, closes, answers wrongly, or nothing listens).
"""

from __future__ import annotations

import socket
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryListenerProbe,
)
from omnimarket.nodes.node_lab_alarm.adapters.runtime_adapters import (
    TcpApiVersionsProbe,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_listener import (
    CONDITION,
    HandlerBrokerListener,
    events_for,
    load_topics,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_broker_listener import (
    EnumBrokerListenerFault,
    EnumBrokerListenerOutcome,
    ModelBrokerListenerCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    ModelBrokerListenerSettings,
    ModelLabAlarmOverlay,
)
from omnimarket.nodes.node_lab_alarm.protocols import (
    ListenerRefusedError,
    ListenerUnresolvableError,
)
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm import (
    build_broker_listener_handler,
    run_broker_listener,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay

pytestmark = pytest.mark.unit

_NODE = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_lab_alarm"
_DELIVER = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lab_alarm_deliver_effect"
)
_CLOCK = datetime(2026, 11, 12, 4, 0, tzinfo=UTC)
_BOOTSTRAP = "broker-a:9092,broker-b:9092"


def _settings(bootstrap: str = _BOOTSTRAP) -> ModelBrokerListenerSettings:
    return ModelBrokerListenerSettings(bootstrap_servers=bootstrap, timeout_seconds=10)


def _command(
    *, write: bool = True, fault: EnumBrokerListenerFault = EnumBrokerListenerFault.NONE
) -> ModelBrokerListenerCommand:
    return ModelBrokerListenerCommand(
        correlation_id="listener-1", write=write, fault=fault
    )


class _Rig:
    def __init__(self, bootstrap: str = _BOOTSTRAP, *, deliver: bool = True) -> None:
        self.probe = InMemoryListenerProbe()
        self.channel = InMemoryAlarmChannel()
        self.ledger = InMemoryAlarmLedger()
        delivery = HandlerLabAlarmDeliver(
            overlay=ModelDeliveryOverlay.model_validate(
                yaml.safe_load(
                    (_DELIVER / "overlay.example.yaml").read_text(encoding="utf-8")
                )
            ),
            channel=self.channel,
            ledger=self.ledger,
            state=InMemoryDeliveryState(),
            clock=lambda: _CLOCK,
        )
        self.handler = HandlerBrokerListener(
            settings=_settings(bootstrap),
            probe=self.probe,
            delivery=delivery if deliver else None,
        )


def test_broker_listener_answering_address_is_ok_and_raises_nothing() -> None:
    rig = _Rig()
    rig.probe.answering.add("broker-b:9092")
    result = rig.handler.handle(_command())
    assert result.outcome is EnumBrokerListenerOutcome.OK
    assert result.alarms == ()
    assert result.failure == ""
    assert rig.probe.calls == ["broker-a:9092", "broker-b:9092"]
    assert "broker-b:9092 answered a Kafka ApiVersions request" in result.evidence
    assert rig.channel.posts == []
    assert rig.ledger.rows == []


def test_broker_listener_first_answering_address_stops_the_dial() -> None:
    rig = _Rig()
    rig.probe.answering.update({"broker-a:9092", "broker-b:9092"})
    rig.handler.handle(_command())
    assert rig.probe.calls == ["broker-a:9092"]


def test_broker_listener_every_address_refused_is_one_alarm() -> None:
    rig = _Rig()
    rig.probe.refused["broker-a:9092"] = "ConnectionRefusedError: refused"
    rig.probe.refused["broker-b:9092"] = "TimeoutError: timed out"
    result = rig.handler.handle(_command(write=False))
    assert result.outcome is EnumBrokerListenerOutcome.ALARM
    [alarm] = result.alarms
    assert alarm.startswith(f"broker-listener:{_BOOTSTRAP}: ")
    assert "did not answer a Kafka ApiVersions request within 10s" in alarm
    assert "broker-a:9092 (ConnectionRefusedError: refused)" in alarm
    assert "broker-b:9092 (TimeoutError: timed out)" in alarm
    assert result.failure == "alarm:broker_listener"
    assert rig.ledger.rows == [], "a probe delivers nothing"


def test_broker_listener_alarm_is_delivered_once_and_ok_ends_the_episode() -> None:
    rig = _Rig()
    first = rig.handler.handle(_command())
    assert first.outcome is EnumBrokerListenerOutcome.ALARM
    assert first.delivery == (
        f"{CONDITION}:broker-listener:{_BOOTSTRAP} channel=delivered ledger=delivered",
    )
    [(_, text)] = rig.channel.posts
    assert _BOOTSTRAP in text
    [(_, row)] = rig.ledger.rows
    assert f"subject=broker-listener:{_BOOTSTRAP}" in row.split(" | ")
    again = rig.handler.handle(_command())
    assert again.delivery == (
        f"{CONDITION}:broker-listener:{_BOOTSTRAP} channel=suppressed ledger=not-due",
    )
    assert len(rig.channel.posts) == 1
    assert len(rig.ledger.rows) == 1
    rig.probe.answering.add("broker-a:9092")
    assert rig.handler.handle(_command()).outcome is EnumBrokerListenerOutcome.OK
    rig.probe.answering.clear()
    rig.handler.handle(_command())
    assert len(rig.channel.posts) == 2
    assert len(rig.ledger.rows) == 1, "the ledger window still holds the first row"


def test_broker_listener_a_failed_delivery_is_named_and_exits_as_an_alarm() -> None:
    rig = _Rig()
    rig.ledger.refuse = "ledger host unreachable"
    failed = rig.handler.handle(_command())
    assert "delivery-failed" in failed.failure
    assert "ledger host unreachable" in failed.failure
    topics = load_topics()
    assert events_for(failed, topics)[-1][0] == topics["broker_listener_run_failed"]


def test_broker_listener_a_drill_alarms_without_dialling_or_delivering() -> None:
    rig = _Rig()
    result = rig.handler.handle(
        _command(fault=EnumBrokerListenerFault.LISTENER_REFUSED)
    )
    assert result.outcome is EnumBrokerListenerOutcome.ALARM
    assert result.fault is EnumBrokerListenerFault.LISTENER_REFUSED
    assert rig.probe.calls == []
    assert result.delivery == ()
    assert rig.channel.posts == []
    assert rig.ledger.rows == []


def test_broker_listener_without_a_delivery_still_reads() -> None:
    rig = _Rig(deliver=False)
    result = rig.handler.handle(_command())
    assert result.outcome is EnumBrokerListenerOutcome.ALARM
    assert result.delivery == ()


def test_broker_listener_golden_and_error_chain_events() -> None:
    topics = load_topics()
    rig = _Rig()
    rig.probe.answering.add("broker-a:9092")
    golden = events_for(rig.handler.handle(_command()), topics)
    assert [t for t, _ in golden] == [
        topics["broker_listener_run_started"],
        topics["broker_listener_run_completed"],
    ]
    rig.probe.answering.clear()
    error = events_for(rig.handler.handle(_command()), topics)
    assert [t for t, _ in error] == [
        topics["broker_listener_run_started"],
        topics["broker_listener_run_failed"],
    ]
    assert error[-1][1]["outcome"] == "ALARM"
    assert error[-1][1]["failure"] == "alarm:broker_listener"


def test_broker_listener_unresolvable_name_is_indeterminate_not_an_alarm() -> None:
    rig = _Rig()
    rig.probe.unresolvable["broker-a:9092"] = "gaierror: not known"
    rig.probe.refused["broker-b:9092"] = "ConnectionRefusedError: refused"
    result = rig.handler.handle(_command())
    assert result.outcome is EnumBrokerListenerOutcome.INDETERMINATE
    assert result.alarms == ()
    assert result.reasons == ("unresolved:broker-a:9092",)
    assert "could not resolve broker-a:9092 (gaierror: not known)" in result.evidence
    assert "refused: broker-b:9092 (ConnectionRefusedError: refused)" in result.evidence
    assert rig.channel.posts == []
    assert rig.ledger.rows == []


def test_broker_listener_unresolvable_does_not_hide_an_answering_address() -> None:
    rig = _Rig()
    rig.probe.unresolvable["broker-a:9092"] = "gaierror: not known"
    rig.probe.answering.add("broker-b:9092")
    assert rig.handler.handle(_command()).outcome is EnumBrokerListenerOutcome.OK


@pytest.mark.parametrize(
    "bootstrap", ["broker-a", "broker-a:", ":9092", "a:1,b:x", "a:9092,"]
)
def test_indeterminate_when_the_bootstrap_string_is_malformed(bootstrap: str) -> None:
    rig = _Rig(bootstrap)
    result = rig.handler.handle(_command())
    assert result.outcome is EnumBrokerListenerOutcome.INDETERMINATE
    assert result.reasons == ("bootstrap-malformed",)
    assert result.failure == "indeterminate:bootstrap-malformed"
    assert rig.probe.calls == [], "a string that cannot be read is never dialled"
    assert result.alarms == ()


def test_indeterminate_is_never_the_healthy_outcome() -> None:
    topics = load_topics()
    rig = _Rig("broker-a")
    result = rig.handler.handle(_command())
    assert events_for(result, topics)[-1][0] == topics["broker_listener_run_failed"]


def test_broker_listener_overlay_loads_without_the_block() -> None:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    assert ModelLabAlarmOverlay.model_validate(raw).broker_listener is not None
    del raw["broker_listener"]
    assert ModelLabAlarmOverlay.model_validate(raw).broker_listener is None


def test_broker_listener_verb_refuses_an_overlay_without_the_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    del raw["broker_listener"]
    code = run_broker_listener(
        "broker-listener",
        EnumBrokerListenerFault.NONE,
        overlay=ModelLabAlarmOverlay.model_validate(raw),
    )
    assert code == 1
    assert "no broker_listener block" in capsys.readouterr().err


def _overlay_with_delivery() -> ModelLabAlarmOverlay:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    return ModelLabAlarmOverlay.model_validate(raw)


def test_broker_listener_cli_exit_codes_follow_the_outcome() -> None:
    overlay = _overlay_with_delivery()
    settings = overlay.broker_listener
    assert settings is not None
    lines: list[str] = []
    probe = InMemoryListenerProbe()
    handler = HandlerBrokerListener(settings=settings, probe=probe)
    assert (
        run_broker_listener(
            "broker-listener-probe",
            EnumBrokerListenerFault.NONE,
            overlay=overlay,
            handler=handler,
            out=lines.append,
        )
        == 2
    )
    assert any(line.startswith("BROKER-LISTENER outcome=ALARM") for line in lines)
    assert any(line.startswith("ALARM broker-listener:") for line in lines)
    probe.answering.add("example-host:9092")
    assert (
        run_broker_listener(
            "broker-listener",
            EnumBrokerListenerFault.NONE,
            overlay=overlay,
            handler=handler,
            out=lines.append,
        )
        == 0
    )
    probe.unresolvable["example-host:9092"] = "gaierror"
    probe.answering.clear()
    assert (
        run_broker_listener(
            "broker-listener",
            EnumBrokerListenerFault.NONE,
            overlay=overlay,
            handler=handler,
            out=lines.append,
        )
        == 3
    )


def test_broker_listener_cli_exits_alarm_when_the_delivery_fails() -> None:
    rig = _Rig()
    rig.ledger.refuse = "ledger host unreachable"
    overlay = _overlay_with_delivery()
    lines: list[str] = []
    code = run_broker_listener(
        "broker-listener",
        EnumBrokerListenerFault.NONE,
        overlay=overlay,
        handler=rig.handler,
        out=lines.append,
    )
    assert code == 2


def test_broker_listener_inmemory_backend_builds_the_fixture_probe() -> None:
    overlay = _overlay_with_delivery()
    assert overlay.broker_listener is not None
    handler = build_broker_listener_handler(overlay, overlay.broker_listener)
    assert handler.handle(_command()).outcome is EnumBrokerListenerOutcome.ALARM


def test_broker_listener_runtime_backend_builds_the_tcp_probe() -> None:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    raw["bus"]["backend"] = "runtime"
    overlay = ModelLabAlarmOverlay.model_validate(raw)
    assert overlay.broker_listener is not None
    handler = build_broker_listener_handler(overlay, overlay.broker_listener)
    assert isinstance(handler._probe, TcpApiVersionsProbe)


# --- the runtime probe against a local socket: the real adapter, no mocks -----------------------------------


def _serve_once(reply: bytes | None) -> tuple[int, threading.Thread]:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def accept() -> None:
        conn, _ = server.accept()
        with conn, server:
            conn.recv(4096)
            if reply is not None:
                conn.sendall(reply)

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    return port, thread


def test_broker_listener_tcp_probe_answers_on_the_right_correlation_id() -> None:
    port, thread = _serve_once((4).to_bytes(4, "big") + (23933).to_bytes(4, "big"))
    TcpApiVersionsProbe().probe("127.0.0.1", port, 5.0)
    thread.join(timeout=5)


def test_broker_listener_tcp_probe_refuses_a_foreign_correlation_id() -> None:
    port, thread = _serve_once((4).to_bytes(4, "big") + (1).to_bytes(4, "big"))
    with pytest.raises(ListenerRefusedError, match="foreign correlation id"):
        TcpApiVersionsProbe().probe("127.0.0.1", port, 5.0)
    thread.join(timeout=5)


def test_broker_listener_tcp_probe_refuses_a_listener_that_closes_unanswered() -> None:
    port, thread = _serve_once(None)
    with pytest.raises(ListenerRefusedError, match="closed before"):
        TcpApiVersionsProbe().probe("127.0.0.1", port, 5.0)
    thread.join(timeout=5)


def test_broker_listener_tcp_probe_refuses_a_port_nothing_listens_on() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.close()
    with pytest.raises(ListenerRefusedError, match="ConnectionRefusedError"):
        TcpApiVersionsProbe().probe("127.0.0.1", port, 5.0)


def test_indeterminate_tcp_probe_reads_a_name_that_cannot_resolve_as_unresolvable() -> (
    None
):
    with pytest.raises(ListenerUnresolvableError):
        TcpApiVersionsProbe().probe("no-such-host.invalid", 9092, 5.0)
