# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain: full content from hook record to session_content row (OMN-19550).

hook record carrying a planted fake credential
  -> the emit effect's fan-out transform for onex.evt.omniclaude.content-captured.v1
  -> node_projection_session_content's writer (the runtime's projection entry)
  -> one row, the credential replaced by its marker, the rest of the text intact
  -> the runtime's terminal event is gated on rows_upserted >= 1
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_emit_daemon.event_registry import (
    TOPIC_SCOPED_TRANSFORM_REGISTRY,
)
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    resolve_event_type,
)
from omnimarket.nodes.node_projection_session_content.handlers.handler_session_content import (
    SessionContentProjectionWriter,
)

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_session_content"
)
TERMINAL = "onex.evt.omnimarket.projection-session-content-applied.v1"
PLANTED = "gh" + "p_" + "FAKE" + "x" * 36


class _RecordingDb:
    def __init__(self) -> None:
        self.rows: list[tuple[Any, ...]] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self.rows.append(args)
        return "INSERT 0 1"


@pytest.mark.unit
def test_golden_chain_hook_record_to_session_content_row() -> None:
    (target,) = resolve_event_type("content.captured")
    transform = TOPIC_SCOPED_TRANSFORM_REGISTRY[target.transform_name]

    hook_record: dict[str, Any] = {
        "session_id": "golden-session",
        "turn_id": "golden-session:turn-1",
        "correlation_id": "golden-session",
        "content_kind": "tool_response",
        "tool_name": "Bash",
        "tool_use_id": "toolu_golden",
        "command": "gh auth token",
        "chunk_index": 0,
        "chunk_count": 1,
        "content": "token is " + PLANTED + " and the build passed",
        "emitted_at": "2026-09-25T12:00:00+00:00",
    }
    published = transform(hook_record, target.topic)

    writer = SessionContentProjectionWriter()
    db = _RecordingDb()
    writer._db = db  # type: ignore[assignment]
    result = writer.handle({**published, "_topic": target.topic})

    assert result["rows_upserted"] == 1
    (row,) = db.rows
    stored = [value for value in row if isinstance(value, str)]
    assert not any(PLANTED in value for value in stored)
    assert "token is [REDACTED:github_token] and the build passed" in stored
    assert "golden-session:turn-1" in stored
    assert "toolu_golden" in stored


@pytest.mark.unit
def test_golden_chain_terminal_event_is_the_declared_applied_topic() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["terminal_event"] == TERMINAL
    assert contract["event_bus"]["publish_topics"] == [TERMINAL]
    assert TERMINAL in contract["externally_consumed_topics"]
