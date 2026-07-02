# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter bus round-trip integration test for the dispatch_engine orchestrator.

WS-5 Wave 7 (OMN-13681), updated for the OMN-13834 router rebuild.
ORCHESTRATOR_GENERIC archetype -> Variant B: the skill-lifecycle handler is
driven over ``EventBusInmemory`` via ``LocalRuntimeBusAdapter``; a skill-request
command is published and the terminal event on the success topic is asserted.

The node is no longer a scaffold: ``handle_skill_requested`` routes through the
real RSD-scoring + self-healing composition (``HandlerDispatchEngineRouter``).
This test asserts the deterministic behavior of the *skill-lifecycle shim* over
the bus:

  * dry_run=True  -> terminal status "dry_run" (short-circuit, no routing).
  * dry_run=False -> terminal status "no_candidates": the bare shim carries no
    backlog access (ticket polling is owned upstream by node_pipeline_fill), so
    routing an empty candidate set is an honest empty cycle — NOT a "dispatched"
    placeholder.
  * args passthrough -> terminal payload echoes the supplied args.
  * malformed skill_path (raw bad fixture) -> rejected at the model boundary,
    NO terminal event published (NEGATIVE CONTROL).

The REAL routed dispatch over a candidate ticket set (worker specs, ranking,
cut accounting) is proven in ``tests/test_dispatch_engine_router.py``.
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
    """Bridge the adapter's ``handle(**payload)`` call to ``handle_skill_requested``.

    ``handle_skill_requested`` is a coroutine; the adapter awaits awaitable
    handler results, so returning the coroutine here drives the async path.
    """

    def __init__(self, event_bus: Any) -> None:
        self._inner = HandlerSkillRequested(event_bus=event_bus)

    def handle(self, **payload: Any) -> Any:
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
        "live-empty-backlog-no_candidates",
        ModelSkillRequest(
            skill_name="merge_sweep",
            skill_path="skills/merge_sweep/SKILL.md",
            dry_run=False,
        ),
        "no_candidates",
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
        "no_candidates",
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
