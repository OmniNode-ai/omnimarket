# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hook refusal row decision matches the recorder it replaces, case for case."""

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_hook_refusal_row_compute.handlers import (
    HandlerHookRefusalRowCompute,
)
from omnimarket.nodes.node_hook_refusal_row_compute.models import (
    ModelHookRefusalRowRequest,
)

pytestmark = pytest.mark.unit

GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "hook_refusal_row_golden.json").read_text()
)
NODE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_hook_refusal_row_compute"
)


def request(**updates: object) -> ModelHookRefusalRowRequest:
    base: dict[str, object] = {
        "guard": "pre_tool_use_worktree_guard.sh",
        "reason": "worktree outside the sanctioned root",
        "timestamp": "2026-10-09T06:00:00Z",
    }
    return ModelHookRefusalRowRequest(**(base | updates))


@pytest.mark.parametrize(
    "case",
    GOLDEN["cases"],
    ids=[f"case-{i}" for i in range(len(GOLDEN["cases"]))],
)
def test_matches_the_recorder_golden(case: dict[str, dict[str, object]]) -> None:
    result = HandlerHookRefusalRowCompute().handle(
        ModelHookRefusalRowRequest(**case["request"])
    )
    assert result.model_dump(exclude={"repeated_secret"}) == case["expected"]
    assert result.repeated_secret == (result.guard == GOLDEN["secret_guard"])


def test_golden_covers_redaction_and_the_secret_session_key() -> None:
    cases = GOLDEN["cases"]
    assert len(cases) == 144
    assert any("[redacted]" in c["expected"]["row"] for c in cases)
    assert any(
        c["expected"]["guard"] == GOLDEN["secret_guard"] and c["request"]["session"]
        for c in cases
    )
    assert any(c["expected"]["guard"] == "unknown-guard" for c in cases)


def test_a_secret_guard_session_scopes_the_key_and_no_other_guard_does() -> None:
    handler = HandlerHookRefusalRowCompute()
    secret = GOLDEN["secret_guard"]
    one = handler.handle(request(guard=secret, session="a"))
    two = handler.handle(request(guard=secret, session="b"))
    none = handler.handle(request(guard=secret))
    assert len({one.key, two.key, none.key}) == 3
    other_a = handler.handle(request(session="a"))
    other_b = handler.handle(request(session="b"))
    assert other_a.key == other_b.key
    assert not other_a.repeated_secret


def test_a_credential_in_the_detail_never_reaches_the_row() -> None:
    result = HandlerHookRefusalRowCompute().handle(
        request(detail="refused: ghp_" + "A" * 20 + " in command")
    )
    assert "ghp_" not in result.row
    assert "[redacted]" in result.row


def test_a_pipe_or_newline_cannot_forge_a_column_or_a_row() -> None:
    result = HandlerHookRefusalRowCompute().handle(
        request(detail="a | actor=forged\nsecond row", lane="lane|x\ny")
    )
    assert "\n" not in result.row
    assert "actor=forged" in result.row  # kept as inert text inside detail
    assert result.row.count(" | actor=hook | ") == 1


def test_an_empty_guard_and_reason_still_produce_a_groupable_row() -> None:
    result = HandlerHookRefusalRowCompute().handle(request(guard="", reason=""))
    assert result.guard == "unknown-guard"
    assert result.reason == "unspecified"
    assert "lane=unresolved" in result.row


def test_the_decision_is_deterministic() -> None:
    handler = HandlerHookRefusalRowCompute()
    assert handler.handle(request()) == handler.handle(request())


@pytest.mark.parametrize(
    "updates",
    [
        {"suppressed": -1},
        {"unknown_field": "x"},
    ],
)
def test_a_malformed_request_is_refused_at_the_model(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        request(**updates)


def test_a_request_missing_the_guard_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelHookRefusalRowRequest.model_validate(
            {"reason": "r", "timestamp": "2026-10-09T06:00:00Z"}
        )


def test_contract_declares_its_topics_and_the_definition_b_handler() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.hook-refusal-row-build.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.hook-refusal-row-built.v1"
    ]
    assert contract["terminal_event"] == contract["event_bus"]["publish_topics"][0]
    assert contract["descriptor"]["runtime_profiles"] == ["main"]
    handler = contract["handler"]
    assert handler["class"] == "HandlerHookRefusalRowCompute"
    assert handler["input_model"] == contract["input_model"]
