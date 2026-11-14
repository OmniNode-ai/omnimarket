# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: a captured tool record in, a classified outcome event out."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_hook_tool_outcome_compute.handlers import (
    HandlerHookToolOutcomeCapture,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.models import (
    EnumToolOutcome,
    ModelHookToolOutcomeClassified,
    ModelToolContentRecord,
    ModelToolOutcomeRules,
)

EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_hook_tool_outcome_compute"
    / "overlay.example.yaml"
)
CORRELATION_ID = "0b4f9d2e-3c1a-4d57-9a6e-5f1c2b7d8e90"


def _raw_rules() -> dict[str, Any]:
    loaded = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _handler(refusal_rules_emptied: bool = False) -> HandlerHookToolOutcomeCapture:
    raw = _raw_rules()
    if refusal_rules_emptied:
        raw["refusal_rules"] = []
    return HandlerHookToolOutcomeCapture(
        rules=ModelToolOutcomeRules.model_validate(raw)
    )


def _record(kind: str, content: str, tool_name: str = "Bash") -> ModelToolContentRecord:
    return ModelToolContentRecord(
        session_id="session-1",
        correlation_id=CORRELATION_ID,
        content_kind=kind,
        tool_name=tool_name,
        content=content,
        emitted_at="2026-11-13T16:00:00Z",
    )


REFUSED_RESPONSE = json.dumps(
    {
        "stdout": "REFUSED factory-floor-publish: the required step failed",
        "stderr": "",
        "interrupted": False,
    }
)


def test_refused_tool_response_names_the_component() -> None:
    event = _handler().handle(_record("tool_response", REFUSED_RESPONSE))
    assert event is not None
    assert event.outcome is EnumToolOutcome.REFUSED
    assert event.refusing_component == "factory-floor-publish"
    assert event.matched_rule == "refused_component_line"
    assert (event.session_id, event.correlation_id, event.tool_name) == (
        "session-1",
        CORRELATION_ID,
        "Bash",
    )
    assert event.emitted_at == "2026-11-13T16:00:00Z"


def test_ok_tool_response_reads_ok() -> None:
    ok = json.dumps({"stdout": "all good", "stderr": "", "interrupted": False})
    event = _handler().handle(_record("tool_response", ok))
    assert event is not None
    assert event.outcome is EnumToolOutcome.OK
    assert event.refusing_component is None


def test_tool_error_with_an_exit_code_reads_failed() -> None:
    event = _handler().handle(_record("tool_error", "Exit code 2\nboom"))
    assert event is not None
    assert event.outcome is EnumToolOutcome.FAILED
    assert event.exit_code == 2


def test_tool_error_with_a_timeout_marker_reads_timeout() -> None:
    event = _handler().handle(_record("tool_error", "Command timed out after 120s"))
    assert event is not None
    assert event.outcome is EnumToolOutcome.TIMEOUT


def test_refusal_wording_from_a_tool_other_than_bash_reads_ok() -> None:
    event = _handler().handle(
        _record("tool_response", "REFUSED x: y", tool_name="Read")
    )
    assert event is not None
    assert event.outcome is EnumToolOutcome.OK


def test_without_the_rules_a_refusal_reads_as_ok() -> None:
    """Failing control: the emptied rule table turns the golden refusal into ok."""
    event = _handler(refusal_rules_emptied=True).handle(
        _record("tool_response", REFUSED_RESPONSE)
    )
    assert event is not None
    assert event.outcome is EnumToolOutcome.OK


def test_classified_event_round_trips_through_its_model() -> None:
    event = _handler().handle(_record("tool_response", REFUSED_RESPONSE))
    assert event is not None
    again = ModelHookToolOutcomeClassified.model_validate(event.model_dump(mode="json"))
    assert again == event
