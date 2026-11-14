# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab_work backend over a directly stated broker address (OMN-24131): the runtime profile.

A node running inside the runtime container has no lane declaration file but holds its own broker
login in its environment, so its overlay names a kafka_bootstrap and no bus_lane. Shape followed:
test_gpu_power_caps_lab_work.py in this directory (a failing control per reason).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.nodes.node_gpu_power_caps_effect.handlers import sender_lab_work_bus
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.handler_gpu_power_caps import (
    HandlerGpuPowerCaps,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    EnumGpuPowerCapsBackend,
    EnumGpuPowerCapsOutcome,
    EnumGpuPowerCapsRefusal,
    ModelGpuPowerCapsRequest,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps_overlay import (
    ModelGpuPowerCapsOverlay,
)
from tests.unit.nodes.node_gpu_power_caps_effect.fixtures import NODE_DIR
from tests.unit.nodes.node_gpu_power_caps_effect.test_gpu_power_caps_lab_work import (
    lab_work_overlay,
    overlay_document,
)

pytestmark = pytest.mark.unit

RUNTIME_EXAMPLE = NODE_DIR / "overlays/lab_work_runtime.example.yaml"


def runtime_overlay_document() -> dict[str, Any]:
    return yaml.safe_load(RUNTIME_EXAMPLE.read_text(encoding="utf-8"))


def runtime_overlay() -> ModelGpuPowerCapsOverlay:
    return ModelGpuPowerCapsOverlay.model_validate(runtime_overlay_document())


def recording_opener_that_refuses(
    recorded: list[dict[str, Any]],
) -> Callable[..., Any]:
    """The bus opener with the bootstrap dropped from its call: what the default opener would
    record if the overlay's address were lost. It refuses, as an unopenable broker does."""

    def opener(**kwargs: Any) -> Any:
        recorded.append(kwargs)
        raise LabRunBusError("no broker for the test")

    return opener


def ask_the_default_sender(
    overlay: ModelGpuPowerCapsOverlay,
    monkeypatch: pytest.MonkeyPatch,
    recorded: list[dict[str, Any]],
) -> Any:
    monkeypatch.setattr(
        sender_lab_work_bus, "open_lane_bus", recording_opener_that_refuses(recorded)
    )
    return HandlerGpuPowerCaps(overlay=overlay).handle(
        ModelGpuPowerCapsRequest(host=overlay.hosts[0].name, requested_by="lane-a")
    )


def test_direct_bootstrap_the_runtime_example_profile_validates_with_a_bootstrap_and_no_lane() -> (
    None
):
    overlay = runtime_overlay()
    lab_work = overlay.lab_work
    assert lab_work is not None
    assert overlay.backend is EnumGpuPowerCapsBackend.LAB_WORK
    assert lab_work.bus_lane is None
    assert lab_work.kafka_bootstrap is not None


def test_direct_bootstrap_the_overlay_names_exactly_one_of_lane_and_bootstrap() -> None:
    both = overlay_document()
    both["lab_work"]["kafka_bootstrap"] = "example-broker:9092"
    with pytest.raises(ValidationError, match="both"):
        ModelGpuPowerCapsOverlay.model_validate(both)
    neither = overlay_document()
    del neither["lab_work"]["bus_lane"]
    with pytest.raises(ValidationError, match="neither"):
        ModelGpuPowerCapsOverlay.model_validate(neither)


def test_direct_bootstrap_the_default_sender_opens_the_bus_at_the_stated_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, Any]] = []
    result = ask_the_default_sender(runtime_overlay(), monkeypatch, recorded)
    lab_work = runtime_overlay().lab_work
    assert lab_work is not None
    assert recorded == [
        {
            "bus": "kafka",
            "lane": None,
            "kafka_bootstrap": lab_work.kafka_bootstrap,
            "root": None,
        }
    ]
    assert result.outcome is EnumGpuPowerCapsOutcome.REFUSED
    assert result.refusal is EnumGpuPowerCapsRefusal.TRANSPORT_UNAVAILABLE
    assert result.detail is not None
    assert "no broker for the test" in result.detail


def test_direct_bootstrap_a_bus_lane_overlay_still_records_the_lane_and_no_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, Any]] = []
    result = ask_the_default_sender(lab_work_overlay(), monkeypatch, recorded)
    lab_work = lab_work_overlay().lab_work
    assert lab_work is not None
    assert lab_work.bus_lane is not None
    assert recorded == [
        {
            "bus": "kafka",
            "lane": lab_work.bus_lane,
            "kafka_bootstrap": None,
            "root": None,
        }
    ]
    assert result.refusal is EnumGpuPowerCapsRefusal.TRANSPORT_UNAVAILABLE
