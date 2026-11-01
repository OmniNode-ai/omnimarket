# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21325: an oversized delegate-skill prompt is refused before any model call.

The eval case P8 was a 300027-byte summarization prompt (62291 input tokens)
that ran in-process and completed, because the only size check lived behind a
per-backend grounding budget that did not apply to the chosen backend. The
bound is now declared in the node contract and enforced in the handler, which
both the in-process port and the deployed consumer run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_prompt_size_bound import (
    load_prompt_size_bound,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegate_skill_orchestrator"
    / "contract.yaml"
)
_P8_PROMPT_BYTES = 300027


class _RecordingPort:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(self, **_kwargs: Any) -> dict[str, object]:
        self.calls += 1
        return {
            "status": "completed",
            "content": "ok",
            "delegated_to": "qwen-coder",
            "model_name": "Qwen3-Coder-30B",
            "quality_gate_passed": True,
            "quality_score": 0.95,
        }


def _handler(port: _RecordingPort) -> HandlerDelegateSkill:
    return HandlerDelegateSkill(dispatch_port=cast("Any", port))


def _request(prompt: str) -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest(
        prompt=prompt,
        task_type="summarization",
        source="claude-code",
        correlation_id=uuid4(),
    )


@pytest.mark.unit
def test_bound_is_declared_in_the_node_contract() -> None:
    declared = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert declared["prompt_size_bound"]["max_prompt_bytes"] == (
        load_prompt_size_bound().max_prompt_bytes
    )
    assert load_prompt_size_bound().max_prompt_bytes < _P8_PROMPT_BYTES


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delegate_prompt_size_bound_refuses_p8_before_any_model_call() -> None:
    port = _RecordingPort()
    handler = _handler(port)

    terminal = await handler.handle(_request("a" * _P8_PROMPT_BYTES))
    assert terminal is not None

    assert terminal.status == "failed"
    assert port.calls == 0
    assert terminal.attempts_count == 0
    assert (
        terminal.terminal_failure_cause
        == EnumDelegationTerminalFailureCause.REFUSED_BEFORE_DISPATCH
    )
    assert terminal.error_message is not None
    assert terminal.error_message.startswith("prompt_exceeds_size_bound:")
    assert f"prompt_bytes={_P8_PROMPT_BYTES}" in terminal.error_message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delegate_prompt_size_bound_counts_bytes_not_characters() -> None:
    port = _RecordingPort()
    handler = _handler(port)
    bound = load_prompt_size_bound().max_prompt_bytes
    # Two-byte characters: fewer characters than the bound, more bytes.
    prompt = "é" * (bound // 2 + 1)
    assert len(prompt) <= bound < len(prompt.encode("utf-8"))

    terminal = await handler.handle(_request(prompt))
    assert terminal is not None

    assert terminal.status == "failed"
    assert port.calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delegate_prompt_size_bound_admits_a_prompt_at_the_bound() -> None:
    port = _RecordingPort()
    handler = _handler(port)
    bound = load_prompt_size_bound().max_prompt_bytes

    terminal = await handler.handle(_request("a" * bound))
    assert terminal is not None

    assert terminal.status == "completed"
    assert port.calls == 1
