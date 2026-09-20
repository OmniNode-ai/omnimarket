# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18852 AC2: the inference rung is bounded, and its abandonment is greppable.

Measured on the ``.201`` dev lane between 19:29Z and 20:11Z on 2026-09-19:
every LLM call on the lane passes through ONE partition served by ONE consumer
member, and ``HandlerInferenceIntent`` awaits the provider call inline inside
it. Two gaps in that window's inference timeline are **exactly 300 s** of dead
slot -- a local Qwen rung dispatched, never answered, and abandoned.

300 s is not a coincidence. The local backend declares ``timeout_ms: 300000``
(``configs/bifrost_delegation.yaml``), the producer turns that into
``intent.timeout_seconds = 300.0``, and the handler resolved its effective
timeout as ``max(1.0, min(600.0, 300.0))`` -- so ONE rung was permitted to run
for the WHOLE of the dispatch port's 300 s wait and for 125 % of the
delegate-skill handler's 240 s execution budget. A rung that cannot finish
inside the budget its caller is measured against cannot produce a usable
answer; it can only hold the single global inference slot until the caller has
already terminalized.

The bound therefore belongs at the EFFECT BOUNDARY, against a ceiling this
node's own contract already declares (``io_operations`` ``http_request``
``timeout_seconds``), and an abandonment must leave a line a single grep finds
in ``docker logs omninode-runtime-effects``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
import yaml
from omnibase_core.models.delegation.wire import ModelInferenceIntent

from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_inference_call_budget import (
    INFERENCE_TIMEOUT_LOG_TOKEN,
    load_inference_call_budget,
)

_EFFECT_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_llm_delegation_call_effect"
    / "contract.yaml"
)
_DELEGATE_SKILL_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegate_skill_orchestrator"
    / "contract.yaml"
)

# The live value the local Qwen backend declares, and the one the producer hands
# this boundary. Written as a literal here on purpose: this test is about what
# the boundary does with a request for 300 s, not about re-deriving it.
_LOCAL_BACKEND_REQUESTED_TIMEOUT_SECONDS = 300.0


def _intent(**overrides: object) -> ModelInferenceIntent:
    defaults: dict[str, object] = {
        "base_url": "http://localhost:8000/v1/chat/completions",
        "model": "Qwen3.8-27B",
        "system_prompt": "You are a helpful assistant.",
        "prompt": "Write a test.",
        "max_tokens": 512,
        "temperature": 0.3,
        "timeout_seconds": _LOCAL_BACKEND_REQUESTED_TIMEOUT_SECONDS,
        "correlation_id": uuid4(),
    }
    defaults.update(overrides)
    return ModelInferenceIntent(**defaults)  # type: ignore[arg-type]


def _mock_client(mock_client_cls: MagicMock, *, post_side_effect: Any = None) -> Any:
    client = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = client
    if post_side_effect is not None:
        client.post.side_effect = post_side_effect
        return client
    response = MagicMock()
    response.json.return_value = {
        "id": "chatcmpl-abc",
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    response.raise_for_status.return_value = None
    client.post.return_value = response
    return client


@pytest.mark.unit
def test_the_provider_call_is_bounded_by_the_contract_ceiling_not_the_request() -> None:
    """RED before the fix: the boundary posts with the requested 300 s.

    The defect in one assertion. A caller whose whole execution budget is 240 s
    cannot be served by a rung permitted to run for 300 s, and the rung holds
    the one global inference slot for every one of those seconds.
    """
    ceiling = float(load_inference_call_budget().max_inference_duration_seconds)
    assert ceiling < _LOCAL_BACKEND_REQUESTED_TIMEOUT_SECONDS

    with patch("httpx.Client") as mock_client_cls:  # onex-allow-faked-boundary
        client = _mock_client(mock_client_cls)
        HandlerInferenceIntent().handle(_intent())

    assert mock_client_cls.call_args.kwargs["timeout"] == ceiling
    assert client.post.call_args.kwargs["timeout"] == ceiling


@pytest.mark.unit
def test_a_request_inside_the_ceiling_is_not_lengthened() -> None:
    """The clamp is a ceiling, never a floor: a 15 s backend still gets 15 s."""
    with patch("httpx.Client") as mock_client_cls:  # onex-allow-faked-boundary
        HandlerInferenceIntent().handle(_intent(timeout_seconds=15.0))

    assert mock_client_cls.call_args.kwargs["timeout"] == 15.0


@pytest.mark.unit
def test_a_timed_out_rung_emits_one_greppable_line_naming_the_correlation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED before the fix: the timeout path logs nothing distinguishable.

    The pre-fix boundary catches ``httpx.TimeoutException`` in the same generic
    ``except Exception`` as every other failure and logs
    ``HandlerInferenceIntent failed: ... error=<str(exc)>``. ``str()`` of an
    httpx timeout is frequently EMPTY, and the line carries neither the elapsed
    time nor the timeout that elapsed -- so a 300 s dead slot is
    indistinguishable from a 0.1 s malformed-payload failure, and there is no
    token to grep for.
    """
    intent = _intent()

    with (
        caplog.at_level(logging.WARNING),
        patch("httpx.Client") as mock_client_cls,  # onex-allow-faked-boundary
    ):
        # The EMPTY message is the live shape, not a contrivance: probed
        # pre-fix, ``str()`` of the timeout raised through the transport was
        # ``''``, so the warning read ``error=`` and the published
        # ``error_message`` was ``''`` -- a failure reporting no error.
        _mock_client(
            mock_client_cls,
            post_side_effect=httpx.TimeoutException(""),
        )
        result = HandlerInferenceIntent().handle(intent)

    # The failure is still returned as an error response, and now says what
    # happened rather than nothing.
    assert result.content == ""
    assert "timed out" in result.error_message
    assert "resolved timeout" in result.error_message

    timeout_lines = [
        record.getMessage()
        for record in caplog.records
        if INFERENCE_TIMEOUT_LOG_TOKEN in record.getMessage()
    ]
    assert len(timeout_lines) == 1, caplog.text
    line = timeout_lines[0]
    assert str(intent.correlation_id) in line
    assert "Qwen3.8-27B" in line
    assert "elapsed_seconds=" in line
    assert "resolved_timeout_seconds=" in line
    assert "requested_timeout_seconds=300.0" in line


@pytest.mark.unit
def test_a_non_timeout_failure_does_not_claim_a_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Positive control on the token: it must not fire on every failure.

    A token emitted on all failures is a token that identifies none of them.
    """
    with (
        caplog.at_level(logging.WARNING),
        patch("httpx.Client") as mock_client_cls,  # onex-allow-faked-boundary
    ):
        _mock_client(
            mock_client_cls,
            post_side_effect=httpx.ConnectError("connection refused"),
        )
        HandlerInferenceIntent().handle(_intent())

    assert INFERENCE_TIMEOUT_LOG_TOKEN not in caplog.text


@pytest.mark.unit
def test_the_ceiling_leaves_room_for_a_second_rung_inside_the_callers_budget() -> None:
    """The cross-node invariant the 300 s value violated.

    ``node_delegate_skill_orchestrator`` terminalizes at
    ``handler_execution_budget.max_handler_duration_seconds``. With a single
    rung permitted 300 s, the escalation ladder could never reach a SECOND rung
    inside that budget -- measured live: correlation ``6c0aaeec`` terminalized
    at 19:51:26 and its cloud rung did not start until 19:57:02. Requiring two
    rungs to fit makes the ladder reachable rather than nominal.
    """
    ceiling = load_inference_call_budget().max_inference_duration_seconds
    raw = yaml.safe_load(_DELEGATE_SKILL_CONTRACT_PATH.read_text(encoding="utf-8"))
    caller_budget = int(raw["handler_execution_budget"]["max_handler_duration_seconds"])

    assert ceiling < caller_budget
    assert 2 * ceiling <= caller_budget


@pytest.mark.unit
def test_loader_refuses_a_contract_that_declares_no_http_timeout(
    tmp_path: Path,
) -> None:
    """Fail loud rather than fall back to a silent default (CLAUDE.md rule 8)."""
    raw = yaml.safe_load(_EFFECT_CONTRACT_PATH.read_text(encoding="utf-8"))
    raw["io_operations"] = [
        entry
        for entry in raw["io_operations"]
        if entry["operation_type"] != "http_request"
    ]
    bad = tmp_path / "contract.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="http_request"):
        load_inference_call_budget(bad)


@pytest.mark.unit
def test_loader_refuses_a_ceiling_that_cannot_bind(tmp_path: Path) -> None:
    """A ceiling at or above the wire model's own maximum clamps nothing.

    ``ModelInferenceIntent.timeout_seconds`` is bounded ``le=600.0``, so a
    declared ceiling of 600 admits every value the wire can carry. That is a
    bound that does not bind, which reads downstream exactly like one that does
    -- the OMN-15504 lesson, applied to the rung instead of the handler.
    """
    raw = yaml.safe_load(_EFFECT_CONTRACT_PATH.read_text(encoding="utf-8"))
    for entry in raw["io_operations"]:
        if entry["operation_type"] == "http_request":
            entry["timeout_seconds"] = 600
    bad = tmp_path / "contract.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="strictly less than"):
        load_inference_call_budget(bad)
