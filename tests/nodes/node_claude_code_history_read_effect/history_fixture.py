# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A recorded Claude Code session, as transcript lines, for OMN-19979 tests."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

SESSION_ID = "11111111-2222-3333-4444-555555555555"
PROJECT = "-home-dev-repo"
ANSWERING_MODEL = "claude-opus-4-6"


def _usage(inp: int, out: int, cc: int = 0, cr: int = 0) -> dict[str, int]:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
    }


def _assistant(
    msg_id: str,
    ts: str,
    block: dict[str, object],
    usage: dict[str, int],
    model: str = ANSWERING_MODEL,
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "sessionId": SESSION_ID,
            "timestamp": ts,
            "message": {
                "id": msg_id,
                "model": model,
                "role": "assistant",
                "content": [block],
                "usage": usage,
            },
        }
    )


def _user(uuid: str, ts: str, content: object, *, is_meta: bool = False) -> str:
    line: dict[str, object] = {
        "type": "user",
        "sessionId": SESSION_ID,
        "uuid": uuid,
        "timestamp": ts,
        "message": {"role": "user", "content": content},
    }
    if is_meta:
        line["isMeta"] = True
    return json.dumps(line)


def session_lines() -> list[str]:
    """Two prompts; the first answered by two messages, the second unanswered.

    Message m1 spans two content-block lines that repeat its usage, as Claude
    Code writes them; it must be counted once. The tool_result line is not a
    prompt, the meta line is not a prompt, the synthetic line is not usage, and
    one line is not JSON.
    """
    first_usage = _usage(1000, 200, cc=50, cr=400)
    return [
        _user("u1", "2026-10-01T10:00:00.000Z", "refactor the parser"),
        _assistant(
            "m1",
            "2026-10-01T10:00:05.000Z",
            {"type": "text", "text": "ok"},
            first_usage,
        ),
        _assistant(
            "m1",
            "2026-10-01T10:00:05.100Z",
            {"type": "tool_use", "name": "Read", "input": {}},
            first_usage,
        ),
        _user(
            "t1",
            "2026-10-01T10:00:06.000Z",
            [{"type": "tool_result", "content": "file"}],
        ),
        _assistant(
            "m2",
            "2026-10-01T10:00:09.000Z",
            {"type": "text", "text": "done"},
            _usage(300, 100),
        ),
        "this line is not json",
        _user(
            "meta1",
            "2026-10-01T10:01:00.000Z",
            "<command-name>/clear</command-name>",
            is_meta=True,
        ),
        _user(
            "u2",
            "2026-10-01T10:02:00.000Z",
            [{"type": "text", "text": "and the tests?"}],
        ),
        _assistant(
            "m3",
            "2026-10-01T10:02:01.000Z",
            {"type": "text", "text": "No response requested."},
            _usage(0, 0),
            model="<synthetic>",
        ),
    ]


class RecordedHistorySource:
    """ProtocolClaudeCodeHistorySource over recorded lines; counts every open."""

    def __init__(
        self, sessions: Iterable[tuple[str, str, list[str]]], *, exists: bool = True
    ) -> None:
        self._sessions = list(sessions)
        self._exists = exists
        self.opened = 0

    def exists(self) -> bool:
        self.opened += 1
        return self._exists

    def iter_sessions(self) -> Iterator[tuple[str, str, Iterable[str]]]:
        self.opened += 1
        yield from self._sessions


def recorded_source() -> RecordedHistorySource:
    return RecordedHistorySource([(PROJECT, SESSION_ID, session_lines())])
