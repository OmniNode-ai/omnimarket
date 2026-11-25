# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression for the clean-machine setup failure recorded on 2026-11-22 (OMN-26451).

The incident's error text is saved under ``tests/fixtures/onboarding_clean_setup_2026_11_22/``. The test replays the
incident's conditions (the forward node's overlay absent, no overlay root named, two recorded setup events) through
the core's own settle step. Before the fix the reading was degraded with the fixture's cause and ``onex setup``
exited 1; after it the reading is healthy.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from omnibase_core.models.event_bus.model_bus_settle import ModelBusSettle
from omnibase_core.models.event_bus.model_runtime_bus_binding import (
    ModelRuntimeBusBinding,
)
from omnibase_core.models.event_bus.model_runtime_bus_event import ModelRuntimeBusEvent

from omnimarket.nodes.node_runtime_bus_forward_effect.adapter_core_forwarder import (
    CoreRuntimeBusForwarder,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.overlay import NODE_NAME

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "fixtures/onboarding_clean_setup_2026_11_22/recorded_setup_error.txt"
)


def test_clean_setup_2026_11_22_reading_does_not_carry_the_recorded_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = FIXTURE.read_text(encoding="utf-8").strip()
    cause = recorded.split("forwarder ", 1)[1]
    assert "NodeOverlayNotFoundError" in cause
    assert "recorded=2" in recorded
    events = tuple(
        ModelRuntimeBusEvent(
            topic=f"onex.evt.omnimarket.setup-{name}.v1",
            key_b64=None,
            value_b64=base64.b64encode(name.encode("utf-8")).decode("ascii"),
            headers={},
        )
        for name in ("started", "completed")
    )
    forwarder = CoreRuntimeBusForwarder(
        environ={}, local_record_dir=tmp_path / "home" / ".onex"
    )
    monkeypatch.setattr(
        ModelRuntimeBusBinding, "load_forward", lambda _self: forwarder.forward
    )
    binding = ModelRuntimeBusBinding(kind="inmemory_forwarded", forwarder=NODE_NAME)

    reading = ModelBusSettle.read(binding, events)

    assert not reading.degraded, reading.reason
    assert reading.events_failed == 0
    assert "NodeOverlayNotFoundError" not in (reading.reason or "")
