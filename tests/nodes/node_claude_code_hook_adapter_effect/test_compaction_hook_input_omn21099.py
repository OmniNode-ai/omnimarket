# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The compaction binding builds a valid command from both hook inputs (OMN-21099).

The 2.1.309 ``session.compact`` event carries ``trigger``, ``instructions``,
``messages`` and ``agentId``; the rendered module adds ``session_id`` and ``cwd``
from ``$.session``. A classic PreCompact command hook carries ``session_id``,
``transcript_path``, ``cwd`` and ``trigger``. Either way the command the contract's
binding builds validates as ModelContextCompactionRequested and its
``input_messages_hash`` is the installed-messages hash of the compacted
conversation's {role, text} list.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from omnimarket.models.context_compaction import ModelContextCompactionRequested
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_claude_code_hook_adapter import (
    INPUT_MESSAGES_HASH_SOURCE,
    build_command_payload,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_client_bindings_loader import (
    CONTRACT_PATH,
    load_client_section,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_input_messages_hash import (
    input_messages_hash,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_session_barrier_journal import (
    HandlerSessionBarrierJournal,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "claude_code_hook_input"
INVOCATION = UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture
def barrier_journal(tmp_path: Path) -> HandlerSessionBarrierJournal:
    return HandlerSessionBarrierJournal(
        environ={"ONEX_HOOK_EMIT_JOURNAL_DIR": str(tmp_path / "journal")}
    )


def _by_hand(pairs: list[tuple[str, str]]) -> str:
    body = json.dumps(
        [{"role": r, "text": t} for r, t in pairs],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _compaction_binding() -> Any:
    section = load_client_section(CONTRACT_PATH)
    return next(b for b in section.bindings if b.name == "compaction")


def test_contract_binding_derives_the_hash_from_the_reserved_source() -> None:
    sources = {a.name: a.source for a in _compaction_binding().arguments}
    assert sources["input_messages_hash"] == INPUT_MESSAGES_HASH_SOURCE
    assert sources["session_id"] == "$.session_id"
    assert sources["cwd"] == "$.cwd"


def test_recorded_precompact_payload_builds_a_valid_command(
    tmp_path: Path, barrier_journal: HandlerSessionBarrierJournal
) -> None:
    transcript = tmp_path / "precompact_transcript.jsonl"
    shutil.copyfile(FIXTURES / "precompact_transcript.jsonl", transcript)
    hook_input = json.loads((FIXTURES / "precompact_manual.json").read_text())
    hook_input["transcript_path"] = str(transcript)

    payload = build_command_payload(
        _compaction_binding(),
        hook_input,
        invocation_id=INVOCATION,
        barrier_journal=barrier_journal,
    )
    command = ModelContextCompactionRequested.model_validate(payload)

    assert command.session_id == hook_input["session_id"]
    assert command.cwd == "/w"
    assert command.trigger.value == "manual"
    # isMeta and non-message lines are skipped; a tool_result-only user line has no
    # text blocks and so reads as an empty text; text blocks join with a newline.
    assert command.input_messages_hash == _by_hand(
        [
            ("user", "Summarise the failing test."),
            ("assistant", "Reading it now.\nThen I will run it."),
            ("user", ""),
        ]
    )


def test_recorded_mod_event_builds_a_valid_command(
    barrier_journal: HandlerSessionBarrierJournal,
) -> None:
    hook_input = json.loads((FIXTURES / "session_compact_mod_event.json").read_text())

    payload = build_command_payload(
        _compaction_binding(),
        hook_input,
        invocation_id=INVOCATION,
        barrier_journal=barrier_journal,
    )
    command = ModelContextCompactionRequested.model_validate(payload)

    assert command.invocation_id == INVOCATION
    assert command.session_id == hook_input["session_id"]
    assert command.cwd == "/w"
    assert command.input_messages_hash == _by_hand(
        [
            ("user", "Summarise the failing test."),
            ("assistant", "Reading it now."),
            ("user", ""),
        ]
    )


def test_messages_win_over_the_transcript(tmp_path: Path) -> None:
    hook_input: dict[str, Any] = {
        "messages": [{"role": "user", "text": "a"}],
        "transcript_path": str(tmp_path / "absent.jsonl"),
    }
    assert input_messages_hash(hook_input) == _by_hand([("user", "a")])


@pytest.mark.parametrize(
    "hook_input",
    [
        {},
        {"transcript_path": ""},
        {"transcript_path": "/nonexistent/transcript.jsonl"},
    ],
)
def test_nothing_derivable_is_none_and_the_command_refuses(
    hook_input: dict[str, Any],
    barrier_journal: HandlerSessionBarrierJournal,
) -> None:
    assert input_messages_hash(hook_input) is None
    payload = build_command_payload(
        _compaction_binding(),
        {"session_id": "s", "cwd": "/w", "trigger": "manual", **hook_input},
        invocation_id=INVOCATION,
        barrier_journal=barrier_journal,
    )
    with pytest.raises(ValueError, match="input_messages_hash"):
        ModelContextCompactionRequested.model_validate(payload)
