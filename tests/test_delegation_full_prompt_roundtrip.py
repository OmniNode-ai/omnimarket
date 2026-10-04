# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation terminal's prompt and response reach delegation_events whole.

The harness emitter used to cut the prompt at 4000 characters before it left
the lane; this guards the other half of the path. A 12000-character prompt and
response projected from a delegate-skill terminal, carrying the emitter's
``prompt_chars`` / ``prompt_sha256`` keys, are read back from the row with
matching length and sha256, so any item can be replayed exactly.
"""

from __future__ import annotations

import hashlib

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

pytestmark = pytest.mark.unit

# Leading and trailing whitespace is part of the prompt: an exact replay needs it.
_PROMPT = "  " + ("prompt line with a stable body\n" * 400)[:11997] + "\n"
_RESPONSE = ("response line with a stable body\n" * 400)[:11999] + "\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_delegate_skill_terminal_row_keeps_a_12000_char_prompt_and_response() -> None:
    assert len(_PROMPT) == len(_RESPONSE) == 12000
    db = InmemoryDatabaseAdapter()
    payload: dict[str, object] = {
        "_db": db,
        "_event_type": "delegate-skill-completed",
        "status": "completed",
        "correlation_id": "6f1c1f0e-8f7a-4f0b-9d55-3a1f6a6f2c01",
        "task_type": "document",
        "provider": "harness:claude-glm",
        "model_name": "glm-5.3",
        "prompt_text": _PROMPT,
        "prompt_chars": len(_PROMPT),
        "prompt_sha256": _sha(_PROMPT),
        "response": _RESPONSE,
        "response_chars": len(_RESPONSE),
        "response_sha256": _sha(_RESPONSE),
        "quality_gate_passed": True,
        "quality_gates_failed": [],
        "metrics": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }

    HandlerProjectionDelegation().handle(payload)

    row = db.query("delegation_events")[0]
    assert len(row["prompt_text"]) == 12000
    assert _sha(str(row["prompt_text"])) == _sha(_PROMPT)
    assert len(row["response_text"]) == 12000
    assert _sha(str(row["response_text"])) == _sha(_RESPONSE)


def test_canonical_terminal_row_keeps_a_12000_char_prompt_and_response() -> None:
    db = InmemoryDatabaseAdapter()
    payload: dict[str, object] = {
        "_db": db,
        "_event_type": "onex.evt.omnibase-infra.delegation-completed.v1",
        "correlation_id": "6f1c1f0e-8f7a-4f0b-9d55-3a1f6a6f2c02",
        "task_type": "document",
        "model_used": "test-model-local",
        "prompt_text": _PROMPT,
        "content": _RESPONSE,
        "quality_passed": True,
    }

    HandlerProjectionDelegation().handle(payload)

    row = db.query("delegation_events")[0]
    assert _sha(str(row["prompt_text"])) == _sha(_PROMPT)
    assert _sha(str(row["response_text"])) == _sha(_RESPONSE)
