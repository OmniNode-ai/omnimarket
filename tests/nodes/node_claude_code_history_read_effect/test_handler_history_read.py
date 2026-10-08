# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_claude_code_history_read_effect: opt-in, one count per message (OMN-19979)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_claude_code_history_read_effect import (
    EnumHistoryReadStatus,
    FilesystemClaudeCodeHistorySource,
    HandlerClaudeCodeHistoryRead,
    ModelClaudeCodeHistoryReadRequest,
    ModelClaudeCodeHistoryReadResult,
)
from tests.nodes.node_claude_code_history_read_effect.history_fixture import (
    ANSWERING_MODEL,
    PROJECT,
    SESSION_ID,
    RecordedHistorySource,
    recorded_source,
    session_lines,
)

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_claude_code_history_read_effect"
)


def _read(
    source: RecordedHistorySource, **kwargs: object
) -> ModelClaudeCodeHistoryReadResult:
    request = ModelClaudeCodeHistoryReadRequest.model_validate(
        {"opt_in": True, "source_root": "/recorded", **kwargs}
    )
    return HandlerClaudeCodeHistoryRead(source_factory=lambda _req: source).handle(
        request
    )


def test_refused_without_opt_in_and_source_untouched() -> None:
    source = recorded_source()
    request = ModelClaudeCodeHistoryReadRequest(source_root="/recorded")
    result = HandlerClaudeCodeHistoryRead(source_factory=lambda _req: source).handle(
        request
    )
    assert result.status is EnumHistoryReadStatus.REFUSED_NO_OPT_IN
    assert result.records == ()
    assert source.opened == 0


def test_missing_source_is_named_not_empty() -> None:
    source = RecordedHistorySource([], exists=False)
    result = _read(source)
    assert result.status is EnumHistoryReadStatus.SOURCE_MISSING
    assert result.records == ()


def test_one_record_per_user_prompt() -> None:
    result = _read(recorded_source())
    assert result.status is EnumHistoryReadStatus.READ
    assert result.files_read == 1
    assert [r.prompt_id for r in result.records] == ["u1", "u2"]
    assert all(
        r.session_id == SESSION_ID and r.project == PROJECT for r in result.records
    )


def test_usage_is_counted_once_per_assistant_message() -> None:
    first = _read(recorded_source()).records[0]
    assert first.model == ANSWERING_MODEL
    assert (first.tokens_in, first.tokens_out) == (1300, 300)
    assert (first.cache_creation_tokens, first.cache_read_tokens) == (50, 400)
    assert first.assistant_messages == 2
    assert first.occurred_at == datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


def test_an_unanswered_prompt_has_no_usage_and_synthetic_lines_are_not_usage() -> None:
    second = _read(recorded_source()).records[1]
    assert second.model == ""
    assert second.has_usage is False
    assert second.assistant_messages == 0


def test_malformed_lines_are_counted_and_skipped() -> None:
    assert _read(recorded_source()).lines_skipped == 1


def test_prompt_text_never_leaves_the_reader() -> None:
    first = _read(recorded_source()).records[0]
    assert first.prompt_chars == len("refactor the parser")
    assert "refactor" not in first.model_dump_json()


def test_window_bounds_filter_by_prompt_time() -> None:
    result = _read(
        recorded_source(),
        since="2026-10-01T10:01:00+00:00",
        until="2026-10-01T11:00:00+00:00",
    )
    assert [r.prompt_id for r in result.records] == ["u2"]


def test_filesystem_source_reads_jsonl_under_the_root(tmp_path: Path) -> None:
    session_dir = tmp_path / PROJECT
    session_dir.mkdir()
    (session_dir / f"{SESSION_ID}.jsonl").write_text("\n".join(session_lines()) + "\n")
    (session_dir / "notes.txt").write_text("not a session")
    request = ModelClaudeCodeHistoryReadRequest(opt_in=True, source_root=str(tmp_path))
    result = HandlerClaudeCodeHistoryRead().handle(request)
    assert result.files_read == 1
    assert [r.prompt_id for r in result.records] == ["u1", "u2"]
    assert result.records[0].project == PROJECT
    source = FilesystemClaudeCodeHistorySource(root=tmp_path, project_filter="no-such")
    assert list(source.iter_sessions()) == []


_TERMINAL_TOPIC = "onex.evt.omnimarket.claude-code-history-read-completed.v1"  # onex-topic-allow: this node's declared terminal


def test_the_contract_declares_its_topics() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    assert contract["terminal_event"] == _TERMINAL_TOPIC
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert contract["terminal_event"] in contract["externally_consumed_topics"]
    assert (
        contract["runtime_dispatch"]["command_topic"]
        in contract["event_bus"]["subscribe_topics"]
    )
