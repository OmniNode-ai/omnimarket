# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick the host installer runs for node_host_job_claim_compute (OMN-25093).

The tick reads the installed units of its host against the node's overlay, publishes the run-completed topic the contract
declares and writes the evidence file the installed unit declares. Every path here is exercised as the unit wires it:
``ONEX_SKILL_OVERLAY_ROOTS`` names the overlay, ``ONEX_LEDGER_HOST_NAME`` names the host, ``--state-dir`` holds the evidence.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.bus_binding import (
    declared_broker_publisher as publisher_module,
)
from omnimarket.nodes.node_host_job_claim_compute import (
    runtime_host_job_claim_tick as tick,
)
from omnimarket.nodes.node_host_job_claim_compute.models import (
    ModelHostJobClaimResult,
    ModelInstalledUnit,
)
from omnimarket.nodes.node_host_job_claim_compute.runtime_host_job_claim import (
    read_claim,
)

pytestmark = pytest.mark.unit

OVERLAY = """\
unit_dirs: [/units]
unit_suffix: .service
roles:
  - role: lab-linux
    jobs:
      - job_id: lane-census
        owner_unit: onex-lane-census
        owner_command_marker: omnimarket.nodes.node_lane_census_effect
        claimants: [onex-lane-census, onex-old-census]
hosts:
  - {host: h051, role: lab-linux}
"""


class Publisher:
    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.sent: list[dict[str, object]] = []

    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: Mapping[str, object],
        event_id: str,
        correlation_id: str | None = None,
        partition_key: str | None = None,
    ) -> bool:
        self.sent.append(
            {"topic": topic, "event_type": event_type, "payload": dict(payload)}
        )
        return self.accepted


class Units:
    def __init__(self, units: tuple[ModelInstalledUnit, ...]) -> None:
        self.units = units

    def installed(
        self, unit_dirs: tuple[str, ...], unit_suffix: str
    ) -> tuple[ModelInstalledUnit, ...]:
        return self.units


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "overlays" / "node_host_job_claim_compute"
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(OVERLAY, encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    monkeypatch.setenv("ONEX_LEDGER_HOST_NAME", "h051")
    return tmp_path / "state"


def _install(
    monkeypatch: pytest.MonkeyPatch, units: tuple[ModelInstalledUnit, ...]
) -> None:
    def read(host: str, correlation_id: str) -> ModelHostJobClaimResult | None:
        return read_claim(host, correlation_id, Units(units))

    monkeypatch.setattr(tick, "read_claim", read)


def test_in_sync_host_publishes_the_run_and_writes_the_evidence(
    wired: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(
        monkeypatch,
        (
            ModelInstalledUnit(
                name="onex-lane-census",
                command="python -m omnimarket.nodes.node_lane_census_effect",
            ),
        ),
    )
    publisher = Publisher()
    code = tick.main(
        ["--omni-home", "/x", "--state-dir", str(wired)], publisher=publisher
    )
    assert code == 0
    assert [s["topic"] for s in publisher.sent] == [
        "onex.evt.omnimarket.host-job-claim-run-completed.v1"
    ]
    evidence = json.loads((wired / "last-run.json").read_text(encoding="utf-8"))
    assert evidence["verdict"] == "IN_SYNC"
    assert evidence["host"] == "h051"
    assert evidence["emitted"] is True
    assert "VERDICT h051: IN_SYNC findings=0 emitted=true" in capsys.readouterr().out


def test_a_second_unit_is_published_and_exits_one(
    wired: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(
        monkeypatch,
        (
            ModelInstalledUnit(
                name="onex-lane-census",
                command="python -m omnimarket.nodes.node_lane_census_effect",
            ),
            ModelInstalledUnit(name="onex-old-census", command="census.sh"),
        ),
    )
    publisher = Publisher()
    code = tick.main(["--state-dir", str(wired)], publisher=publisher)
    assert code == tick.EXIT_DRIFTED
    payload = publisher.sent[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["findings"][0]["extra_unit"] == "onex-old-census"
    evidence = json.loads((wired / "last-run.json").read_text(encoding="utf-8"))
    assert evidence["verdict"] == "DRIFTED"


def test_a_bus_that_refuses_the_record_is_indeterminate(
    wired: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing control: a run whose result never reached the bus must not read as a clean run."""
    _install(monkeypatch, ())
    code = tick.main(["--state-dir", str(wired)], publisher=Publisher(accepted=False))
    assert code == tick.EXIT_INDETERMINATE
    evidence = json.loads((wired / "last-run.json").read_text(encoding="utf-8"))
    assert evidence["emitted"] is False


def test_the_probe_reads_only_the_overlay(
    wired: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    publisher = Publisher()
    assert tick.main(["--probe"], publisher=publisher) == 0
    assert capsys.readouterr().out == "PROBE ok\n"
    assert publisher.sent == []
    assert not wired.exists()


def test_a_missing_overlay_is_indeterminate_and_leaves_the_reason_in_the_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing control: the state of the dev runtime before the overlay was declared."""
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "empty"))
    monkeypatch.setenv("ONEX_LEDGER_HOST_NAME", "h051")
    state = tmp_path / "state"
    assert tick.main(["--state-dir", str(state)], publisher=Publisher()) == 3
    evidence = json.loads((state / "last-run.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "overlay_unreadable"


def test_a_host_with_no_declared_role_exits_two(
    wired: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_LEDGER_HOST_NAME", "h999")
    publisher = Publisher()
    assert (
        tick.main(["--state-dir", str(wired)], publisher=publisher) == tick.EXIT_NO_ROLE
    )
    assert publisher.sent == []
    evidence = json.loads((wired / "last-run.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "no_declared_role"


def test_the_event_the_tick_publishes_is_registered_for_the_contract_topic() -> None:
    """Seam: the emit effect refuses a topic whose event type the registry does not declare (read on h051, 2026-11-17)."""
    root = Path(tick.__file__).resolve().parents[1]
    registry = yaml.safe_load(
        (root / "node_emit_daemon/registries/topics.yaml").read_text(encoding="utf-8")
    )
    fan_out = registry["events"][tick.EVENT_TYPE]["fan_out"]
    topic = tick._contract()["topics"]["host_job_claim_run_completed"]
    assert [row["topic"] for row in fan_out] == [topic]


def test_the_tick_publishes_under_the_registered_event_type(
    wired: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, ())
    publisher = Publisher()
    assert tick.main(["--state-dir", str(wired)], publisher=publisher) == 0
    assert publisher.sent[0]["event_type"] == tick.EVENT_TYPE


def test_the_overlay_may_declare_the_bus_the_tick_publishes_on(
    wired: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_host_job_claim_compute.overlay import load_overlay

    overlay_file = (
        wired.parent / "overlays" / "node_host_job_claim_compute" / "overlay.yaml"
    )
    assert load_overlay().event_bus is None
    overlay_file.write_text(
        OVERLAY + "event_bus:\n  type: kafka\n  lane: dev\n", encoding="utf-8"
    )
    declared = load_overlay().event_bus
    assert declared is not None
    assert (declared.type, declared.lane) == ("kafka", "dev")


def test_the_declared_broker_publisher_names_no_broker_setting_in_the_unit_env(
    wired: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An overlay with no event_bus names no broker, and a KAFKA_BOOTSTRAP_SERVERS in the environment does not stand in for it.
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "ignored.invalid:9092")
    monkeypatch.setenv("OMNI_HOME", str(wired.parent))
    # The shared default overlay declares no broker either, so the node's own refusal stands.
    (wired.parent / "overlays" / "run_node_default").mkdir(parents=True, exist_ok=True)
    (wired.parent / "overlays" / "run_node_default" / "overlay.yaml").write_text(
        "node: run_node_default\n", encoding="utf-8"
    )
    accepted = tick.DeclaredBrokerPublisher().publish(
        topic="onex.evt.omnimarket.host-job-claim-run-completed.v1",
        event_type=tick.EVENT_TYPE,
        payload={"host": "h051"},
        event_id="e1",
    )
    assert accepted is False
    assert "declares no event_bus" in capsys.readouterr().err


def test_the_declared_broker_publisher_sends_the_envelope_on_the_declared_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aiokafka

    from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker

    sent: list[tuple[str, bytes]] = []
    started: dict[str, object] = {}

    class Producer:
        def __init__(self, **kwargs: object) -> None:
            started.update(kwargs)

        async def start(self) -> None: ...

        async def stop(self) -> None: ...

        async def send_and_wait(self, topic: str, value: bytes) -> None:
            sent.append((topic, value))

    monkeypatch.setattr(aiokafka, "AIOKafkaProducer", Producer)
    monkeypatch.setattr(
        publisher_module,
        "resolve_dialable_broker",
        lambda _node: ModelNodeBroker(
            kind="kafka", lane="dev", bootstrap_servers="broker.invalid:9092"
        ),
    )
    accepted = tick.DeclaredBrokerPublisher().publish(
        topic="onex.evt.omnimarket.host-job-claim-run-completed.v1",
        event_type=tick.EVENT_TYPE,
        payload={"host": "h051", "verdict": "IN_SYNC"},
        event_id="e1",
        correlation_id="c1",
    )
    assert accepted is True
    assert started["bootstrap_servers"] == "broker.invalid:9092"
    assert len(sent) == 1
    envelope = json.loads(sent[0][1])
    assert envelope["event_type"] == tick.EVENT_TYPE
    assert envelope["correlation_id"] == "c1"
    assert envelope["payload"]["host"] == "h051"
