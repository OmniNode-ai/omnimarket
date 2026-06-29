# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter bus round-trip integration test for the dispatch_engine orchestrator.

WS-5 Wave 7 (OMN-13681). ORCHESTRATOR_GENERIC archetype -> Variant B: the
handler is driven over ``EventBusInmemory`` via ``LocalRuntimeBusAdapter``; a
skill-request command is published and the terminal event on the success topic
is asserted.

HONESTY NOTE: ``node_skill_dispatch_engine_orchestrator`` is a documented
scaffold (``contract.yaml`` ``maturity: stub``, OMN-8821) — live dispatch to the
polymorphic agent is follow-up work, so the handler returns a ``"dispatched"``
placeholder. This test does NOT fake that follow-up; it asserts the REAL,
deterministic behavior that exists today: the dry_run vs live status branch, the
args passthrough, and the input-validation boundary that rejects malformed
skill requests (negative control -> no terminal event published).

Param axes (>=3 distinct sets + a negative control):
  * dry_run=True  -> terminal status "dry_run".
  * dry_run=False -> terminal status "dispatched".
  * args passthrough -> terminal payload echoes the supplied args.
  * malformed skill_path (raw bad fixture) -> rejected at the model boundary,
    NO terminal event reaches the success topic  (NEGATIVE CONTROL).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.handlers.handler_skill_requested import (
    HandlerSkillRequested,
)
from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.models import (
    ModelSkillRequest,
)
from tests.integration._wave7_bus import drive_round_trip

_START_TOPIC = "onex.cmd.omnimarket.dispatch_engine.v1"
_SUCCESS_TOPIC = "onex.evt.omnimarket.dispatch_engine-completed.v1"


class _DispatchHandlerWrapper:
    """Bridge the adapter's ``handle(**payload)`` call to ``handle_skill_requested``."""

    def __init__(self, event_bus: Any) -> None:
        self._inner = HandlerSkillRequested(event_bus=event_bus)

    def handle(self, **payload: Any) -> dict[str, Any]:
        return self._inner.handle_skill_requested(**payload)


# (case_id, command, expected_status, expected_args)
_VALID_CASES: list[tuple[str, ModelSkillRequest, str, dict[str, str]]] = [
    (
        "dry-run-returns-dry_run",
        ModelSkillRequest(
            skill_name="merge_sweep",
            skill_path="skills/merge_sweep/SKILL.md",
            dry_run=True,
        ),
        "dry_run",
        {},
    ),
    (
        "live-returns-dispatched",
        ModelSkillRequest(
            skill_name="merge_sweep",
            skill_path="skills/merge_sweep/SKILL.md",
            dry_run=False,
        ),
        "dispatched",
        {},
    ),
    (
        "args-passthrough",
        ModelSkillRequest(
            skill_name="gap",
            skill_path="skills/gap/SKILL.md",
            args={"scope": "post-merge", "severity": "high"},
            dry_run=False,
        ),
        "dispatched",
        {"scope": "post-merge", "severity": "high"},
    ),
]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_id", "command", "expected_status", "expected_args"),
    _VALID_CASES,
    ids=[c[0] for c in _VALID_CASES],
)
async def test_dispatch_engine_round_trip(
    integration_event_bus: Any,
    case_id: str,
    command: ModelSkillRequest,
    expected_status: str,
    expected_args: dict[str, str],
) -> None:
    history = await drive_round_trip(
        integration_event_bus,
        handler=_DispatchHandlerWrapper(event_bus=integration_event_bus),
        handler_name="dispatch-engine",
        input_model_cls=ModelSkillRequest,
        start_topic=_START_TOPIC,
        output_topic=_SUCCESS_TOPIC,
        payload_bytes=command.model_dump_json().encode("utf-8"),
        group_id=f"dispatch-engine-test-{case_id}",
    )

    assert len(history) == 1, f"{case_id}: expected exactly one terminal event"
    payload = json.loads(history[0].value)
    assert payload["status"] == expected_status
    assert payload["skill_name"] == command.skill_name
    assert payload["skill_path"] == command.skill_path
    assert payload["args"] == expected_args


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dispatch_engine_rejects_malformed_skill_path(
    integration_event_bus: Any,
) -> None:
    """NEGATIVE CONTROL: a skill_path not ending in SKILL.md is rejected at the
    deserialization boundary, so NO terminal event is published."""
    bad_payload = json.dumps(
        {
            "skill_name": "merge_sweep",
            "skill_path": "skills/merge_sweep/NOT_A_SKILL.txt",
            "dry_run": False,
        }
    ).encode("utf-8")

    history = await drive_round_trip(
        integration_event_bus,
        handler=_DispatchHandlerWrapper(event_bus=integration_event_bus),
        handler_name="dispatch-engine",
        input_model_cls=ModelSkillRequest,
        start_topic=_START_TOPIC,
        output_topic=_SUCCESS_TOPIC,
        payload_bytes=bad_payload,
        group_id="dispatch-engine-test-bad-path",
    )

    assert history == [], "malformed skill request must not emit a terminal event"
