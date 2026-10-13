# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The frozen automation-liveness event fixtures as one ordered stream.

The overlay the seam fixture declares holds one process. The stream adds a
second, declared but never emitted, so a test can tell a declared silent
process from an absent one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from omnimarket.models.liveness.model_automation_liveness import (
    EVENT_PAYLOAD_MODELS,
    EnumAutomationLivenessEvent,
    ModelAutomationLivenessOverlay,
    automation_liveness_topics,
)
from omnimarket.nodes.node_projection_automation_liveness.models import (
    AutomationLivenessEvent,
)

_EVENT_ADAPTER: TypeAdapter[AutomationLivenessEvent] = TypeAdapter(
    AutomationLivenessEvent
)

EVENTS_DIR = Path(__file__).resolve().parents[1] / "fixtures/automation_liveness/events"

ACTIVE_PROCESS = "host-a/example-interval-job"
ACTIVE_HOST = "host-a"
SILENT_PROCESS = "host-b/example-silent-job"
SILENT_HOST = "host-b"

#: The order a live deployment produces them: declaration first, the run and
#: its verdict, the alarm lifecycle last.
STREAM_ORDER = (
    EnumAutomationLivenessEvent.LIVENESS_DECLARED,
    EnumAutomationLivenessEvent.RUN_OBSERVED,
    EnumAutomationLivenessEvent.HEARTBEAT,
    EnumAutomationLivenessEvent.LIVENESS_VERDICT,
    EnumAutomationLivenessEvent.ALARM_RAISED,
    EnumAutomationLivenessEvent.ALARM_DELIVERED,
    EnumAutomationLivenessEvent.ALARM_RECORDED,
    EnumAutomationLivenessEvent.ALARM_CLEARED,
)


def fixture_payload(kind: EnumAutomationLivenessEvent) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(
        (EVENTS_DIR / f"{kind.value}.json").read_text(encoding="utf-8")
    )
    if kind is EnumAutomationLivenessEvent.LIVENESS_DECLARED:
        return _with_silent_process(payload)
    return payload


def _with_silent_process(payload: dict[str, Any]) -> dict[str, Any]:
    processes: list[dict[str, Any]] = payload["overlay"]["processes"]
    silent = {
        **processes[0],
        "process_id": SILENT_PROCESS,
        "host": SILENT_HOST,
        "trigger": {"kind": "launchd-interval", "native_id": "com.example.silent-job"},
    }
    overlay = ModelAutomationLivenessOverlay.model_validate(
        {**payload["overlay"], "processes": [*processes, silent]}
    )
    return {
        **payload,
        "overlay": overlay.model_dump(mode="json"),
        "overlay_digest": overlay.digest(),
    }


def fixture_event(kind: EnumAutomationLivenessEvent) -> AutomationLivenessEvent:
    model = EVENT_PAYLOAD_MODELS[kind]
    return _EVENT_ADAPTER.validate_python(model.model_validate(fixture_payload(kind)))


def fixture_stream() -> list[
    tuple[EnumAutomationLivenessEvent, AutomationLivenessEvent]
]:
    return [(kind, fixture_event(kind)) for kind in STREAM_ORDER]


def fixture_topic(kind: EnumAutomationLivenessEvent) -> str:
    return automation_liveness_topics()[kind]
