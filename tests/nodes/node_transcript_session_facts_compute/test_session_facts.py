# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_transcript_session_facts_compute: counts, subagent streams, determinism, revision, no prompt text (OMN-22259, T1)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from omnimarket.models.work_classification import (
    EnumRecordType,
    EnumStreamKind,
    ModelSessionFacts,
    ModelSourceRevision,
)
from omnimarket.nodes.node_transcript_session_facts_compute.handlers.handler_transcript_session_facts import (
    HandlerTranscriptSessionFacts,
)
from omnimarket.nodes.node_transcript_session_facts_compute.models import (
    ModelSessionFactsRequest,
    SourceRevisionShortError,
)

pytestmark = pytest.mark.unit

SID = "013d2012-829d-46aa-955f-458c3f747ee7"
TOKEN = "zq-unique-prompt-token-7731"
PR_URL = "https://github.com/OmniNode-ai/omnimarket/pull/4999"


def _jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)


def _base(**extra: Any) -> dict[str, Any]:
    return {"sessionId": SID, "timestamp": "2026-11-02T10:00:00.000Z", **extra}


def fixture_records() -> list[dict[str, Any]]:
    """One record of each type the plan lists, plus the sidechain Write and the deny decision."""
    return [
        _base(
            type="user",
            uuid="u1",
            cwd="/work/repo",
            gitBranch="omn-1-slug",
            entrypoint="cli",
            promptSource="terminal",
            message={"role": "user", "content": f"please do {TOKEN}"},
        ),
        _base(
            type="assistant",
            uuid="a1",
            effort="high",
            message={
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [
                    {"type": "text", "text": "ok"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
        ),
        _base(
            type="assistant",
            uuid="a2",
            isSidechain=True,
            agentId="agent-x1",
            message={
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Write",
                        "input": {"file_path": "/work/repo/out.txt", "content": "x"},
                    }
                ],
            },
        ),
        _base(type="system", subtype="compact_boundary", content="compacted"),
        _base(type="system", subtype="stop_hook_summary", content="hooks ran"),
        _base(type="system", subtype="api_error", content="overloaded"),
        _base(type="pr-link", prUrl=PR_URL, prNumber=4999),
        _base(type="cost-state", costUSD=1.25),
        _base(
            type="attachment",
            attachment={"type": "hook_decision", "permissionDecision": "deny"},
        ),
        _base(type="summary", summary="a summary"),
        _base(type="file-history-snapshot"),
    ]


def facts_of(text: str, **kw: Any) -> ModelSessionFacts:
    return HandlerTranscriptSessionFacts().handle(
        ModelSessionFactsRequest(transcript_text=text, **kw)
    )


def test_each_record_type_is_counted_to_the_fixture() -> None:
    facts = facts_of(_jsonl(fixture_records()))
    assert facts.session_id == SID
    assert facts.stream_kind is EnumStreamKind.SESSION
    assert facts.cwd == "/work/repo"
    assert facts.git_branch == "omn-1-slug"
    assert facts.entrypoint == "cli"
    assert facts.effort == "high"
    assert facts.models == ("claude-opus-5", "claude-sonnet-5-5")
    assert facts.record_counts[EnumRecordType.USER.value] == 1
    assert facts.record_counts[EnumRecordType.ASSISTANT.value] == 2
    assert facts.record_counts[EnumRecordType.PR_LINK.value] == 1
    assert facts.record_counts[EnumRecordType.COST_STATE.value] == 1
    assert facts.pr_link_count == 1
    assert facts.pr_link_urls == (PR_URL,)
    assert facts.cost_state_count == 1
    assert facts.compact_boundary_count == 1
    assert facts.hook_summary_count == 1
    assert facts.api_error_count == 1
    assert facts.sidechain_record_count == 1
    assert facts.tool_call_counts == {"Bash": 1, "Write": 1}
    assert facts.written_paths == ("/work/repo/out.txt",)
    assert facts.permission_decision_counts == {"deny": 1}
    assert facts.unknown_record_types == {}
    assert facts.undecodable_line_count == 0


def test_an_unknown_record_type_is_counted_not_failed() -> None:
    records = [
        *fixture_records(),
        _base(type="brand-new-kind"),
        _base(type="brand-new-kind"),
    ]
    facts = facts_of(_jsonl(records))
    assert facts.unknown_record_types == {"brand-new-kind": 2}


def test_a_subagent_file_yields_a_subagent_stream_with_the_parents_session_id() -> None:
    records = [
        _base(
            type="user", isSidechain=True, agentId="agent-x1", message={"content": "go"}
        ),
        _base(
            type="assistant",
            isSidechain=True,
            agentId="agent-x1",
            message={
                "model": "m",
                "content": [
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "/a"}}
                ],
            },
        ),
    ]
    facts = facts_of(_jsonl(records))
    assert facts.stream_kind is EnumStreamKind.SUBAGENT
    assert facts.agent_id == "agent-x1"
    assert facts.session_id == SID


def test_the_facts_are_deterministic_and_a_one_byte_tool_edit_changes_exactly_that_count() -> (
    None
):
    text = _jsonl(fixture_records())
    first, second = facts_of(text), facts_of(text)
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()
    edited = text.replace('"name": "Bash"', '"name": "Bush"')
    assert len(edited) == len(text)
    changed = facts_of(edited)
    assert changed.tool_call_counts == {"Bush": 1, "Write": 1}
    a, b = first.model_dump(), changed.model_dump()
    differing = {k for k in a if a[k] != b[k]}
    assert differing == {"tool_call_counts"}


def test_a_longer_read_than_the_scan_satisfies_the_close_and_a_shorter_one_fails() -> (
    None
):
    records = fixture_records()
    ten, eleven = records[:10], records[:11]
    required = facts_of(_jsonl(ten)).source_revision
    assert required.record_count == 10
    got = facts_of(_jsonl(eleven), required_source_revision=required)
    assert got.source_revision.record_count == 11
    assert got.source_revision.satisfies(required)
    with pytest.raises(SourceRevisionShortError):
        facts_of(_jsonl(records[:9]), required_source_revision=required)


def test_source_revision_orders_by_record_count_and_ignores_timestamps() -> None:
    records = fixture_records()
    shorter = facts_of(_jsonl(records[:-1])).source_revision
    full = facts_of(_jsonl(records)).source_revision
    assert shorter < full
    earlier = {**records[0], "timestamp": "2000-01-01T00:00:00.000Z", "uuid": "late"}
    appended = facts_of(_jsonl([*records, earlier])).source_revision
    assert appended > full
    assert isinstance(full, ModelSourceRevision)


def test_byte_length_breaks_a_tie_on_record_count() -> None:
    small = ModelSourceRevision(record_count=3, byte_length=10)
    big = ModelSourceRevision(record_count=3, byte_length=11)
    assert small < big
    assert ModelSourceRevision(record_count=4, byte_length=1) > big


def test_no_prompt_text_appears_in_any_serialized_output() -> None:
    facts = facts_of(_jsonl(fixture_records()))
    assert TOKEN not in facts.model_dump_json()
    assert TOKEN not in repr(facts)
    assert (
        facts.first_prompt_sha256
        == hashlib.sha256(f"please do {TOKEN}".encode()).hexdigest()
    )


def test_blank_and_undecodable_lines_are_counted_and_do_not_fail() -> None:
    text = _jsonl(fixture_records()[:2]) + "not json\n\n"
    facts = facts_of(text)
    assert facts.undecodable_line_count == 1
    assert facts.source_revision.record_count == 3
