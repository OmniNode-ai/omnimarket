# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hand-flip parity for the three baselined projection nodes OMN-21288 rebinds.

Each contract now binds an in-process writer that hands one runtime delivery to
the node's existing projection runner. The runner's ``project_event`` is the
unchanged business logic; these tests pin the adapter boundary: the bound class
dispatches in-process, wraps that runner, and passes the delivery's topic,
payload and source coordinates through to it exactly once.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

from omnimarket.nodes.node_projection_live_events.handlers.handler_live_events import (
    HandlerLiveEventsProjectionRunner,
)
from omnimarket.nodes.node_projection_registration.handlers.handler_registration import (
    RegistrationProjectionRunner,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
    SavingsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit

NODES = Path(__file__).parents[1] / "src/omnimarket/nodes"

RUNNERS: dict[str, type[Any]] = {
    "node_projection_registration": RegistrationProjectionRunner,
    "node_projection_savings": SavingsProjectionRunner,
    "node_projection_live_events": HandlerLiveEventsProjectionRunner,
}


class _RecordingRunner:
    """Stands in for the projection runner; records each runtime delivery."""

    calls: ClassVar[list[tuple[str, dict[str, Any], MessageMeta]]] = []

    async def project_runtime_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        type(self).calls.append((topic, data, meta))
        return True


def _bound_handler_class(node: str) -> type[Any]:
    contract = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    binding = contract["handler"]
    module = importlib.import_module(binding["module"])
    cls: type[Any] = getattr(module, binding["class"])
    return cls


@pytest.mark.parametrize("node", sorted(RUNNERS))
def test_bound_handler_wraps_the_existing_runner(node: str) -> None:
    cls = _bound_handler_class(node)
    assert getattr(cls, "onex_runtime_inprocess_dispatch", False) is True, (
        f"{node} contract handler {cls.__name__} is not an in-process writer"
    )
    assert getattr(cls, "runner_type", None) is RUNNERS[node]


@pytest.mark.parametrize("node", sorted(RUNNERS))
def test_bound_handler_delivers_one_event_to_the_runner(
    node: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cls = _bound_handler_class(node)
    assert getattr(cls, "onex_runtime_inprocess_dispatch", False) is True, (
        f"{node} contract handler {cls.__name__} is not an in-process writer"
    )
    monkeypatch.setattr(cls, "runner_type", _RecordingRunner)
    monkeypatch.setattr(_RecordingRunner, "calls", [])

    result = cls().handle(
        {
            "_db": object(),
            "_topic": "onex.evt.platform.node-heartbeat.v1",
            "_partition": 2,
            "_offset": 41,
            "node_id": "n-1",
        }
    )

    assert result == {"projected": True}
    assert len(_RecordingRunner.calls) == 1
    topic, data, meta = _RecordingRunner.calls[0]
    assert topic == "onex.evt.platform.node-heartbeat.v1"
    assert data == {"node_id": "n-1"}
    assert (meta.topic, meta.partition, meta.offset) == (topic, 2, 41)
