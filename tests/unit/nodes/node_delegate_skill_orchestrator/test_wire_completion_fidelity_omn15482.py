# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""End-to-end completion-shaping fidelity on the delegation wire (OMN-15482).

Three parameters a direct OpenAI-compatible chat-completions caller takes for
granted -- a system/user role split, a sampling ``temperature``, and a
``response_format`` directive -- had no counterpart on
``ModelDelegateSkillRequest``. A consumer migrating off a direct HTTP provider
binding onto the delegation path therefore changed behaviour SILENTLY: the
temperature was dropped, the two chat roles collapsed into one concatenated
``prompt`` string, and JSON mode degraded into an appended prompt sentence.

These tests drive the FULL seam a wire-level caller actually exercises -- the
OMN-14208 cross-boundary discipline, one chain through REAL components rather
than three independent unit suites:

    ModelDelegateSkillRequest(system_prompt=..., temperature=..., response_format=...)
      -> HandlerDelegateSkill.handle()
      -> LocalDelegationDispatchPort.dispatch(...)
      -> ModelLlmDelegationCallRequest
      -> HandlerLlmDelegationCall._execute_call()
      -> the outbound chat-completions HTTP payload

The last hop is the one that matters and the one previous seam tests stopped
short of: the assertions below read the payload dict handed to
``transport.post_chat_completion``, so "the role split survives" and
"temperature is forwarded" are claims about the bytes that leave the process,
not about an intermediate model field.

Only two things are faked: ``load_bifrost_backends`` (an in-memory backends
list, so the test does not depend on live overlay/store secrets -- the same
fixture shape OMN-15180/OMN-15193 use) and the HTTP transport itself. Every
model, the handler, the dispatch port, and the effect handler run for real.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as local_port_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    _TASK_TYPE_SYSTEM_PROMPTS,
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing import delegation_backend_resolution

_MLX_ENDPOINT = "http://stickybeatz-studio:8401/v1/chat/completions"
_MLX_MODEL_ID = "mlx-community/Qwen3.6-35B-A3B-8bit"

_TACTICAL_RESPONSE_CONTRACT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "action_params": {"type": "object"},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["action", "action_params", "confidence", "rationale"],
}

_TACTICAL_RESPONSE = (
    '{"action": "advance", "action_params": {"unit_id": "u-1"}, '
    '"confidence": 0.9, "rationale": "The flank is clear."}'
)

_STEEL_SYSTEM_PROMPT = "You are a tactical pilot. Reply with one JSON decision."
_STEEL_USER_PROMPT = "Enemy at grid 4,7. Choose an action."


def _backends() -> list[dict[str, Any]]:
    return [
        {
            "backend_id": "local-coder-mlx",
            "endpoint_url": _MLX_ENDPOINT,
            "model_name": _MLX_MODEL_ID,
            "tier": "local",
            "max_tokens": 65536,
            "timeout_ms": 300000,
            "capabilities": ["agent_delegation"],
        },
    ]


class _RecordingEffect:
    """Injected effect handler recording the typed effect request it receives."""

    def __init__(self) -> None:
        self.calls: list[ModelLlmDelegationCallRequest] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_TACTICAL_RESPONSE,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


def _make_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[HandlerDelegateSkill, _RecordingEffect]:
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: _backends(),
    )
    effect = _RecordingEffect()
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
    )
    return HandlerDelegateSkill(dispatch_port=port), effect


def _steel_shaped_request(**overrides: Any) -> ModelDelegateSkillRequest:
    kwargs: dict[str, Any] = {
        "prompt": _STEEL_USER_PROMPT,
        "task_type": "agent_delegation",
        "source": "external-client",
        "backend_id": "local-coder-mlx",
        "response_contract": _TACTICAL_RESPONSE_CONTRACT,
    }
    kwargs.update(overrides)
    return ModelDelegateSkillRequest(**kwargs)


# ---------------------------------------------------------------------------
# Wire-model surface
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_wire_model_accepts_the_three_completion_shaping_fields() -> None:
    """The gap OMN-15482 closes: before this change ``extra="forbid"`` made all
    three of these a hard ValidationError, which is what made an overlay
    migration off a direct HTTP binding non-behaviour-preserving."""
    request = _steel_shaped_request(
        system_prompt=_STEEL_SYSTEM_PROMPT,
        temperature=0.7,
        response_format={"type": "json_object"},
    )

    assert request.system_prompt == _STEEL_SYSTEM_PROMPT
    assert request.temperature == 0.7
    assert request.response_format == {"type": "json_object"}


@pytest.mark.unit
def test_wire_model_defaults_all_three_to_none() -> None:
    """Every pre-existing caller keeps its exact shape: no new required field."""
    request = _steel_shaped_request()

    assert request.system_prompt is None
    assert request.temperature is None
    assert request.response_format is None


@pytest.mark.unit
@pytest.mark.parametrize("temperature", [-0.1, 2.1])
def test_wire_model_rejects_out_of_range_temperature(temperature: float) -> None:
    with pytest.raises(ValidationError):
        _steel_shaped_request(temperature=temperature)


@pytest.mark.unit
@pytest.mark.parametrize(
    "response_format",
    [
        {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}},
        {"type": "text"},
        {"type": "json_object", "strict": True},
        {},
    ],
)
def test_wire_model_rejects_unsupported_response_format(
    response_format: dict[str, Any],
) -> None:
    """Fail-loud, not permissive. Accepting a directive this path does not
    actually thread (notably OpenAI ``json_schema`` structured output) would
    reintroduce the same silent-fidelity class in a new place: the caller would
    believe it had constrained the response when it had not."""
    with pytest.raises(ValidationError):
        _steel_shaped_request(response_format=response_format)


# ---------------------------------------------------------------------------
# Seam: wire model -> handler -> port -> effect request
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_handler_propagates_completion_shaping_to_effect_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each of the three parameters stopping at any hop is the silent-drop
    defect this ticket exists to close, so pin all three on the effect request
    the port actually builds."""
    handler, effect = _make_handler(tmp_path, monkeypatch)

    response = await handler.handle(
        _steel_shaped_request(
            system_prompt=_STEEL_SYSTEM_PROMPT,
            temperature=0.7,
            response_format={"type": "json_object"},
        )
    )

    assert response.status == "completed"
    assert len(effect.calls) == 1
    call = effect.calls[0]
    assert call.system_prompt == _STEEL_SYSTEM_PROMPT
    assert call.temperature == 0.7
    assert call.response_format == {"type": "json_object"}
    # The user prompt is NOT concatenated with the system prompt. The only thing
    # this backend's inference profile prepends is its own protocol directive
    # (``/no_think`` for the MLX Qwen family) -- backend shaping, which applied
    # identically before OMN-15482 and is not the caller's system prompt.
    assert call.prompt.endswith(_STEEL_USER_PROMPT)
    assert _STEEL_SYSTEM_PROMPT not in call.prompt


@pytest.mark.unit
async def test_caller_system_prompt_replaces_the_task_type_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller-supplied system prompt REPLACES the task-type default rather
    than being appended anywhere -- otherwise the migrated caller would silently
    inherit an extra persona it never asked for."""
    handler, effect = _make_handler(tmp_path, monkeypatch)

    await handler.handle(
        _steel_shaped_request(
            task_type="code_generation", system_prompt=_STEEL_SYSTEM_PROMPT
        )
    )

    call = effect.calls[0]
    assert call.system_prompt == _STEEL_SYSTEM_PROMPT
    assert _TASK_TYPE_SYSTEM_PROMPTS["code_generation"] not in call.system_prompt


@pytest.mark.unit
async def test_omitting_all_three_preserves_pre_existing_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-regression half: a request that sets none of the three produces
    exactly the pre-OMN-15482 effect request -- task-type default system
    prompt, the effect model's own default temperature, no response_format."""
    handler, effect = _make_handler(tmp_path, monkeypatch)

    await handler.handle(_steel_shaped_request(task_type="code_generation"))

    call = effect.calls[0]
    assert call.system_prompt == _TASK_TYPE_SYSTEM_PROMPTS["code_generation"]
    assert (
        call.temperature
        == ModelLlmDelegationCallRequest.model_fields["temperature"].default
    )
    assert call.response_format is None


@pytest.mark.unit
def test_default_call_temperature_tracks_the_effect_model_default() -> None:
    """``_DEFAULT_CALL_TEMPERATURE`` is derived from the effect model, never
    restated as a literal, so "None preserves pre-existing behaviour" cannot
    quietly become false if that default is ever changed.

    Resolved via ``getattr`` rather than a module-level import so this file
    still IMPORTS against pre-OMN-15482 source -- that is what makes the
    RED-before control behavioural (assertions fail) instead of a collection
    error that proves only that a new private constant did not exist yet.
    """
    default_call_temperature = getattr(
        local_port_module, "_DEFAULT_CALL_TEMPERATURE", None
    )
    assert default_call_temperature is not None, (
        "_DEFAULT_CALL_TEMPERATURE is missing: the port is restating the effect "
        "model's temperature default instead of deriving it"
    )
    assert (
        default_call_temperature
        == ModelLlmDelegationCallRequest.model_fields["temperature"].default
    )


@pytest.mark.unit
def test_response_format_is_reserved_against_inference_profile_overrides() -> None:
    """One producer per wire key. ``response_format`` is now caller-owned, so an
    inference profile writing it through ``provider_request_options`` must fail
    loud rather than silently win."""
    assert "response_format" in local_port_module._RESERVED_PROVIDER_REQUEST_KEYS


# ---------------------------------------------------------------------------
# Seam: effect request -> outbound HTTP payload (the real wire boundary)
# ---------------------------------------------------------------------------


def _outbound_payload_for(
    call_request: ModelLlmDelegationCallRequest, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Run the REAL effect handler and capture the payload it POSTs."""
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
        handler_llm_delegation_call as effect_module,
    )

    captured: dict[str, Any] = {}

    def _fake_post(*, endpoint_url: str, payload: dict[str, Any], **_: Any) -> Any:
        captured.update(payload)
        return effect_module.transport.ModelTransportResponse(
            status_code=200,
            json_body={
                "choices": [{"message": {"content": _TACTICAL_RESPONSE}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 22},
            },
            latency_ms=5,
        )

    monkeypatch.setattr(effect_module.transport, "post_chat_completion", _fake_post)
    monkeypatch.setattr(effect_module, "_is_endpoint_healthy", lambda _url: True)

    handler = effect_module.HandlerLlmDelegationCall()
    handler.handle(call_request)
    assert captured, "the effect handler never POSTed a payload"
    return captured


def _effect_request(**overrides: Any) -> ModelLlmDelegationCallRequest:
    kwargs: dict[str, Any] = {
        "request_id": "r-1",
        "correlation_id": "c-1",
        "causation_id": "c-1",
        "model_id": _MLX_MODEL_ID,
        "endpoint_ref": _MLX_ENDPOINT,
        "prompt": _STEEL_USER_PROMPT,
        "prompt_hash": "",
        "system_prompt": _STEEL_SYSTEM_PROMPT,
        "task_type": "agent_delegation",
        "max_tokens": 512,
        "timeout_seconds": 30.0,
    }
    kwargs.update(overrides)
    return ModelLlmDelegationCallRequest(**kwargs)


@pytest.mark.unit
def test_outbound_payload_carries_two_distinct_chat_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2 at the wire-payload boundary: the backend receives TWO messages with
    distinct roles, not one concatenated string."""
    payload = _outbound_payload_for(_effect_request(), monkeypatch)

    assert payload["messages"] == [
        {"role": "system", "content": _STEEL_SYSTEM_PROMPT},
        {"role": "user", "content": _STEEL_USER_PROMPT},
    ]


@pytest.mark.unit
def test_outbound_payload_carries_caller_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _outbound_payload_for(_effect_request(temperature=0.7), monkeypatch)

    assert payload["temperature"] == 0.7


@pytest.mark.unit
def test_outbound_payload_carries_response_format_as_a_wire_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 at the wire-payload boundary: JSON mode is a payload key, and the
    prompt text is untouched by it."""
    payload = _outbound_payload_for(
        _effect_request(response_format={"type": "json_object"}), monkeypatch
    )

    assert payload["response_format"] == {"type": "json_object"}
    assert payload["messages"][1]["content"] == _STEEL_USER_PROMPT


@pytest.mark.unit
def test_outbound_payload_omits_response_format_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-regression: an unset directive adds no key at all, so the outbound
    body is byte-identical to the pre-OMN-15482 one for every existing caller."""
    payload = _outbound_payload_for(_effect_request(), monkeypatch)

    assert "response_format" not in payload


# ---------------------------------------------------------------------------
# The deployed bus path carries completion shaping on the canonical wire
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("system_prompt", "You are a tactical pilot."),
        ("temperature", 0.7),
        ("response_format", {"type": "json_object"}),
    ],
)
async def test_runtime_bus_port_publishes_completion_shaping_without_dropping(
    field: str, value: Any
) -> None:
    """The managed-cloud path must publish every non-None fidelity field."""
    from omnibase_core.models.delegation.wire import ModelDelegationRequest
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
        RuntimeDelegationDispatchPort,
    )

    class _CapturingBus:
        def __init__(self) -> None:
            self.published: list[bytes] = []

        async def publish(
            self, _topic: str, _key: bytes | None, value: bytes, _headers: object
        ) -> None:
            self.published.append(value)

    bus = _CapturingBus()
    port = RuntimeDelegationDispatchPort(event_bus=bus)

    await port.dispatch(
        prompt=_STEEL_USER_PROMPT,
        task_type="agent_delegation",
        correlation_id=uuid4(),
        max_tokens=512,
        source_file_path=None,
        source_session_id=None,
        wait=False,
        quality_contract_mode="extend_task_class",
        acceptance_criteria=(),
        tenant_id=None,
        **{field: value},
    )

    assert len(bus.published) == 1
    request = (
        ModelEventEnvelope[ModelDelegationRequest]
        .model_validate_json(bus.published[0])
        .payload
    )
    assert getattr(request, field) == value


# ---------------------------------------------------------------------------
# Seam guard: every port implementation accepts the whole protocol
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("port_name", ["local", "runtime"])
def test_every_dispatch_port_accepts_the_full_protocol_signature(
    port_name: str,
) -> None:
    """Widening ``ProtocolDelegationDispatchPort`` is only safe if every
    implementation moves with it -- and a lagging implementation fails in a
    genuinely misleading way.

    ``HandlerDelegateSkill.handle()`` wraps the dispatch call in a broad
    ``except Exception`` that turns any error into
    ``ModelDelegateSkillResponse(status="failed")``. So a port missing one of
    these keyword parameters does NOT raise ``TypeError: unexpected keyword
    argument`` to the caller -- it surfaces as a plausible-looking FAILED
    delegation, with the real cause only in ``error_message``. That is the
    silent-wiring-death class, and it is exactly what this widening made easier
    to hit (observed for real while landing OMN-15482: two test stubs still on
    the old signature reported ``status='failed'`` rather than erroring).

    This test compares parameter names structurally, so a future widening that
    updates the Protocol but not a port fails HERE, loudly, instead of
    downstream as a mystery FAILED status.
    """
    import inspect

    from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
        ProtocolDelegationDispatchPort,
    )
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
        RuntimeDelegationDispatchPort,
    )

    port_cls: type = (
        LocalDelegationDispatchPort
        if port_name == "local"
        else RuntimeDelegationDispatchPort
    )
    required = set(
        inspect.signature(ProtocolDelegationDispatchPort.dispatch).parameters
    )
    actual = set(inspect.signature(port_cls.dispatch).parameters)

    missing = required - actual
    assert not missing, (
        f"{port_cls.__name__}.dispatch is missing protocol parameters "
        f"{sorted(missing)}; HandlerDelegateSkill always passes them and the "
        "resulting TypeError would be swallowed into status='failed'"
    )


# ---------------------------------------------------------------------------
# Contract declares what the model accepts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_declares_the_three_completion_shaping_inputs() -> None:
    """A wire field the contract does not declare is a contract-first violation
    even when the Pydantic model accepts it."""
    import yaml

    contract_path = (
        Path(__file__).resolve().parents[4]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_delegate_skill_orchestrator"
        / "contract.yaml"
    )
    inputs = yaml.safe_load(contract_path.read_text())["inputs"]

    assert inputs["system_prompt"]["required"] is False
    assert inputs["temperature"]["required"] is False
    assert inputs["temperature"]["minimum"] == 0.0
    assert inputs["temperature"]["maximum"] == 2.0
    assert inputs["response_format"]["required"] is False


@pytest.mark.unit
def test_steel_payload_validates_against_the_wire_model() -> None:
    """Cross-repo seam check (OMN-14208), and the reason the subprocess hop is
    safe to leave uncovered.

    This is the EXACT payload ``steel_onslaught.llm.client_delegation.
    LlmBusDelegationClient.complete()`` writes to its ``--input`` file, key for
    key. If steel adds, renames, or retypes a payload key, this fails here --
    on the consuming side -- rather than at runtime inside the CLI.

    **This test lives in THIS module, not the live-backend one, and that
    placement is load-bearing.** It shipped in
    ``test_live_completion_fidelity_omn15482.py``, whose module-level
    ``pytestmark = pytest.mark.skipif(OMN_ALLOW_LIVE_LADDER != "1")`` applies to
    every test in that file. A per-test ``@pytest.mark.unit`` does NOT cancel a
    module-level ``skipif``, so this pin -- the one both halves of OMN-15482
    cite as the mitigation for the uncovered subprocess hop -- was SKIPPED in
    CI and pinned nothing. Verified skipped on the merge commit ``fb99acf2``
    before this move. Do not relocate it back into a live-gated module.
    """
    steel_payload: dict[str, Any] = {
        "prompt": "Enemy contact at grid 4,7. What do you do?",
        "system_prompt": "You are a mech pilot.",
        "temperature": 0.7,
        "task_type": "agent_delegation",
        "source": "external-client",
        "correlation_id": "11111111-2222-3333-4444-555555555555",
        "backend_id": "local-coder-mlx",
        "response_contract": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "minLength": 1},
                "action_params": {"type": "object"},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "rationale": {"type": "string", "minLength": 1},
            },
            "required": ["action", "action_params", "confidence", "rationale"],
            "additionalProperties": False,
        },
        "response_format": {"type": "json_object"},
        "max_tokens": 4096,
    }

    request = ModelDelegateSkillRequest.model_validate(steel_payload)

    assert request.system_prompt == "You are a mech pilot."
    assert request.temperature == 0.7
    assert request.response_format == {"type": "json_object"}
    assert request.backend_id == "local-coder-mlx"


@pytest.mark.unit
def test_no_unit_marked_test_hides_behind_a_module_level_skipif() -> None:
    """Mechanism, not a rule: the defect this module's seam pin was moved out of
    is detectable, so it cannot silently recur in a sibling module.

    ``@pytest.mark.unit`` on a test inside a module whose ``pytestmark`` carries
    a ``skipif`` is always a mistake -- the marker reads as "runs in CI" while
    the module-level condition skips it. Scanned statically (no import, no
    collection) across this node's test package.
    """
    import ast

    offenders: list[str] = []
    for path in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
        tree = ast.parse(path.read_text())

        module_skips = any(
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
            )
            and "skipif" in ast.dump(node.value)
            for node in tree.body
        )
        if not module_skips:
            continue

        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if any(
                "unit" in ast.dump(dec) and "mark" in ast.dump(dec)
                for dec in node.decorator_list
            ):
                offenders.append(f"{path.name}::{node.name}")

    assert offenders == [], (
        "these tests are marked `unit` but sit under a module-level skipif, so "
        f"they do not run in CI: {offenders}"
    )
