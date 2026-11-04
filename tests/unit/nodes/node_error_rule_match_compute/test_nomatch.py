# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise the existing queue verb, and the decision/write crash boundary."""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.error_rule_match import (
    ModelErrorRuleNoMatch,
    ModelErrorRuleNoMatchOverlay,
    ModelErrorRuleNoMatchRequest,
)
from omnimarket.nodes.node_error_rule_nomatch_effect import NodeErrorRuleNoMatchEffect


def request(tmp_path: Path):
    return ModelErrorRuleNoMatchRequest(
        decision=ModelErrorRuleNoMatch(
            class_key="tool.Bash.template.abc", key_version="f" * 64
        ),
        overlay=ModelErrorRuleNoMatchOverlay(
            state_dir=tmp_path / "queue",
            emit_node="node_exception_queue_effect",
            class_prefix="error-miss-",
            class_hash_length=40,
            source="error-rule-match",
            tried="exact class-key lookup",
            proposed_fix="triage the missing class",
        ),
        evidence="uncovered fixture cause",
        now="2026-11-04T00:00:00Z",
        repository="omnimarket",
    )


def test_no_match_exception_emits_one_case_via_existing_queue(tmp_path):
    handler = NodeErrorRuleNoMatchEffect()
    command = request(tmp_path)
    first = handler.handle(command)
    second = handler.handle(command.model_copy(update={"now": "2026-11-04T00:01:00Z"}))
    rows = [
        json.loads(line)
        for line in (command.overlay.state_dir / "exception-queue.jsonl")
        .read_text()
        .splitlines()
    ]
    assert first.queued.status == "appended"
    assert second.queued.status == "repeat"
    assert first.queued.id == second.queued.id
    assert len(rows) == 1
    assert rows[0]["key"] == command.decision.class_key
    assert (
        rows[0]["cls"]
        == command.overlay.class_prefix
        + hashlib.sha256(command.decision.class_key.encode()).hexdigest()[:40]
    )
    assert rows[0]["evidence"] == command.evidence


def test_no_match_exception_crash_before_emit_reruns_without_lost_row(tmp_path):
    class CrashBeforeWrite:
        def handle(self, request):
            raise OSError("simulated crash before queue write")

    command = request(tmp_path)
    with pytest.raises(OSError, match="simulated crash"):
        NodeErrorRuleNoMatchEffect(emit=CrashBeforeWrite()).handle(command)
    assert not command.overlay.state_dir.exists()
    result = NodeErrorRuleNoMatchEffect().handle(command)
    assert result.queued.status == "appended"
    assert (
        len(
            (command.overlay.state_dir / "exception-queue.jsonl")
            .read_text()
            .splitlines()
        )
        == 1
    )


def test_match_cannot_enter_no_match_effect(tmp_path):
    command = request(tmp_path).model_dump(mode="json")
    command["decision"]["status"] = "MATCH"
    with pytest.raises(ValidationError):
        ModelErrorRuleNoMatchRequest.model_validate(command)
