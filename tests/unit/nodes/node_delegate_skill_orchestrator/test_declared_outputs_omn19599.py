# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation that declares output files gets a manifest on its terminal (OMN-19599).

The delegate-skill request declares ``declared_outputs``; the orchestrator asks
the model for the declared files through the existing ``response_contract``
seam, runs the pure extract compute on the accepted reply, and stamps
``output_manifest`` and ``output_files`` on the completed terminal. Nothing is
written to disk here: the caller materializes (OMN-19601), and the projection
stores one row per file.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.delegation.declared_outputs_contract import (
    build_declared_outputs_response_contract,
    render_declared_outputs_instruction,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
)
from omnimarket.models.delegation.wire.model_delegation_output_files import (
    EnumDelegationOutputFileRefusalReason,
    ModelDeclaredOutputs,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)

pytestmark = pytest.mark.unit

_DECLARED = ModelDeclaredOutputs.model_validate(
    {
        "files": [
            {"path": "src/slug.py", "kind": "code"},
            {"path": "tests/test_slug.py", "kind": "test"},
        ]
    }
)
_SLUG_PY = "def slug(value: str) -> str:\n    return value.lower()\n"
_TEST_PY = (
    "from slug import slug\n\n\ndef test_slug() -> None:\n    assert slug('A') == 'a'\n"
)


def _reply(*files: tuple[str, str]) -> str:
    return json.dumps({"files": [{"path": p, "content": c} for p, c in files]})


class _ReplyPort:
    """A dispatch port that records what it was asked and answers with ``content``."""

    def __init__(self, content: str, *, status: str = "completed") -> None:
        self._content = content
        self._status = status
        self.kwargs: dict[str, Any] = {}

    async def dispatch(self, **kwargs: Any) -> dict[str, object]:
        self.kwargs = kwargs
        return {
            "status": self._status,
            "content": self._content,
            "delegated_to": "local-coder",
            "model_name": "test-model",
            "quality_gate_passed": self._status == "completed",
            "quality_score": 0.95,
            **({"error_message": "boom"} if self._status == "failed" else {}),
        }


def _request(**extra: object) -> ModelDelegateSkillRequest:
    return ModelDelegateSkillRequest.model_validate(
        {
            "prompt": "write a slug helper and its test",
            "task_type": "code_generation",
            "correlation_id": uuid4(),
            "source": "external-client",
            **extra,
        }
    )


async def _run(
    port: _ReplyPort, request: ModelDelegateSkillRequest
) -> ModelDelegateSkillCompleted | ModelDelegateSkillFailed | None:
    handler = HandlerDelegateSkill(dispatch_port=port, idempotency_port=None)
    return await handler.handle(request)


@pytest.mark.asyncio
async def test_completed_terminal_carries_a_manifest_whose_sha256_is_the_stored_bytes() -> (
    None
):
    port = _ReplyPort(
        _reply(("src/slug.py", _SLUG_PY), ("tests/test_slug.py", _TEST_PY))
    )
    terminal = await _run(port, _request(declared_outputs=_DECLARED))

    assert isinstance(terminal, ModelDelegateSkillCompleted)
    assert terminal.output_manifest is not None
    assert terminal.output_files is not None
    assert terminal.output_manifest.refusals == ()
    by_path = {e.path: e for e in terminal.output_manifest.entries}
    assert set(by_path) == {"src/slug.py", "tests/test_slug.py"}
    for stored in terminal.output_files:
        entry = by_path[stored.path]
        assert entry.sha256 == hashlib.sha256(stored.content_bytes()).hexdigest()
        assert entry.size_bytes == len(stored.content_bytes())
        assert entry.artifact_ref == f"sha256:{entry.sha256}"


@pytest.mark.asyncio
async def test_the_model_is_asked_for_the_declared_files_through_the_response_contract() -> (
    None
):
    port = _ReplyPort(
        _reply(("src/slug.py", _SLUG_PY), ("tests/test_slug.py", _TEST_PY))
    )
    await _run(port, _request(declared_outputs=_DECLARED))

    assert port.kwargs["response_contract"] == build_declared_outputs_response_contract(
        _DECLARED
    )
    assert render_declared_outputs_instruction(_DECLARED) in port.kwargs["prompt"]
    assert "write a slug helper and its test" in port.kwargs["prompt"]


@pytest.mark.asyncio
async def test_a_declared_file_the_reply_omits_is_a_named_refusal_not_a_silent_gap() -> (
    None
):
    port = _ReplyPort(_reply(("src/slug.py", _SLUG_PY)))
    terminal = await _run(port, _request(declared_outputs=_DECLARED))

    assert isinstance(terminal, ModelDelegateSkillCompleted)
    assert terminal.output_manifest is not None
    assert [e.path for e in terminal.output_manifest.entries] == ["src/slug.py"]
    assert [(r.path, r.reason) for r in terminal.output_manifest.refusals] == [
        ("tests/test_slug.py", EnumDelegationOutputFileRefusalReason.MISSING)
    ]


@pytest.mark.asyncio
async def test_a_failed_terminal_carries_no_manifest() -> None:
    port = _ReplyPort("", status="failed")
    terminal = await _run(port, _request(declared_outputs=_DECLARED))

    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.output_manifest is None
    assert terminal.output_files is None


@pytest.mark.asyncio
async def test_an_ordinary_request_is_untouched_and_its_terminal_omits_the_keys() -> (
    None
):
    port = _ReplyPort("plain answer")
    request = _request()
    terminal = await _run(port, request)

    assert isinstance(terminal, ModelDelegateSkillCompleted)
    assert port.kwargs["prompt"] == request.prompt
    assert port.kwargs["response_contract"] is None
    dumped = terminal.model_dump(mode="json")
    assert "output_manifest" not in dumped
    assert "output_files" not in dumped
    assert "declared_outputs" not in request.model_dump(mode="json")


def test_declared_outputs_and_a_caller_response_contract_are_refused_together() -> None:
    with pytest.raises(ValidationError) as caught:
        _request(declared_outputs=_DECLARED, response_contract={"type": "object"})
    assert "declared_outputs" in str(caught.value)


@pytest.mark.asyncio
async def test_the_terminal_round_trips_through_the_wire() -> None:
    port = _ReplyPort(
        _reply(("src/slug.py", _SLUG_PY), ("tests/test_slug.py", _TEST_PY))
    )
    request = _request(declared_outputs=_DECLARED)
    wire_request = ModelDelegateSkillRequest.model_validate_json(
        request.model_dump_json()
    )
    assert wire_request.declared_outputs == _DECLARED

    terminal = await _run(port, wire_request)
    assert isinstance(terminal, ModelDelegateSkillCompleted)
    decoded = ModelDelegateSkillCompleted.model_validate_json(
        terminal.model_dump_json()
    )
    assert decoded.output_manifest == terminal.output_manifest
    assert decoded.output_files == terminal.output_files
