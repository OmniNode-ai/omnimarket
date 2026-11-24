# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A machine that declares no hosted bus keeps its setup events locally; a declared one that fails is named (OMN-26451).

``onex setup`` runs its node on the in-memory bus and the core runtime settles that bus through the forwarder
registered under ``onex.runtime_bus_forwarders``. Both cases go through the core's own ``ModelBusSettle.read``,
the step that decides whether the run's events were delivered:

- no overlay root and no overlay for the forward node: the machine declares no hosted bus, the forwarder keeps the
  events in a local record file, and the reading is healthy and says so;
- an overlay is declared but its hosted bus cannot be reached: the reading is degraded, names the hosted bus and
  the error, and does not say the events were kept locally.

The unit-test conftest stands in for core's delivery check, so these tests read the settle step itself.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import yaml
from omnibase_core.models.event_bus.model_bus_settle import ModelBusSettle
from omnibase_core.models.event_bus.model_runtime_bus_binding import (
    ModelRuntimeBusBinding,
)
from omnibase_core.models.event_bus.model_runtime_bus_event import ModelRuntimeBusEvent
from omnibase_core.models.event_bus.model_runtime_bus_reading import (
    ModelRuntimeBusReading,
)

from omnimarket.nodes.node_runtime_bus_forward_effect import (
    adapter_core_forwarder as forwarder_module,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.adapter_core_forwarder import (
    CoreRuntimeBusForwarder,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.handlers.handler_runtime_bus_local_record import (
    LOCAL_RECORD_FILE_NAME,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.overlay import NODE_NAME

pytestmark = pytest.mark.unit

OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_runtime_bus_forward_effect/overlay.example.yaml"
)
UNREACHABLE = "the hosted bus broker is not reachable"


def _events() -> tuple[ModelRuntimeBusEvent, ...]:
    return tuple(
        ModelRuntimeBusEvent(
            topic=topic,
            key_b64=None,
            value_b64=base64.b64encode(value.encode("utf-8")).decode("ascii"),
            headers={},
        )
        for topic, value in (
            ("onex.evt.omnimarket.setup-started.v1", "one"),
            ("onex.evt.omnimarket.setup-completed.v1", "two"),
        )
    )


def _settle(
    forwarder: CoreRuntimeBusForwarder, monkeypatch: pytest.MonkeyPatch
) -> ModelRuntimeBusReading:
    binding = ModelRuntimeBusBinding(kind="inmemory_forwarded", forwarder=NODE_NAME)
    monkeypatch.setattr(
        ModelRuntimeBusBinding, "load_forward", lambda _self: forwarder.forward
    )
    return ModelBusSettle.read(binding, _events())


def _no_overlay_forwarder(tmp_path: Path) -> CoreRuntimeBusForwarder:
    return CoreRuntimeBusForwarder(
        environ={}, local_record_dir=tmp_path / "home" / ".onex"
    )


def _unreachable_publisher(_broker: object) -> object:
    raise OSError(UNREACHABLE)


def _declared_forwarder(tmp_path: Path) -> CoreRuntimeBusForwarder:
    root = tmp_path / "overlay_root"
    (root / NODE_NAME).mkdir(parents=True)
    document = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    (root / NODE_NAME / "overlay.yaml").write_text(yaml.safe_dump(document))
    return CoreRuntimeBusForwarder(
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(root)},
        publisher_for_broker=_unreachable_publisher,  # type: ignore[arg-type]
        local_record_dir=tmp_path / "home" / ".onex",
    )


def test_no_hosted_bus_declared_is_not_a_failure_and_keeps_events_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reading = _settle(_no_overlay_forwarder(tmp_path), monkeypatch)

    assert not reading.degraded
    assert reading.events_recorded == 2
    assert reading.events_failed == 0
    assert reading.reason is not None
    assert "no hosted bus is declared" in reading.reason
    assert "kept locally" in reading.reason
    record = tmp_path / "home" / ".onex" / LOCAL_RECORD_FILE_NAME
    kept = [json.loads(line) for line in record.read_text().splitlines()]
    assert [event["topic"] for event in kept] == [
        "onex.evt.omnimarket.setup-started.v1",
        "onex.evt.omnimarket.setup-completed.v1",
    ]
    assert record.stat().st_mode & 0o777 == 0o600


def test_declared_hosted_bus_that_is_unreachable_is_named_and_not_called_kept_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        forwarder_module, "broker_for_lane", lambda lane, _environ: lane
    )

    reading = _settle(_declared_forwarder(tmp_path), monkeypatch)

    assert reading.degraded
    assert reading.events_failed == 2
    assert reading.reason is not None
    assert "could not reach the hosted bus" in reading.reason
    assert UNREACHABLE in reading.reason
    assert "kept locally" not in reading.reason
    assert not (tmp_path / "home" / ".onex" / LOCAL_RECORD_FILE_NAME).exists()


def test_a_local_record_that_cannot_be_written_is_degraded_not_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the record directory should be")
    forwarder = CoreRuntimeBusForwarder(environ={}, local_record_dir=blocked / ".onex")

    reading = _settle(forwarder, monkeypatch)

    assert reading.degraded
    assert reading.reason is not None
    assert "no hosted bus is declared" in reading.reason
    assert "could not be written" in reading.reason
