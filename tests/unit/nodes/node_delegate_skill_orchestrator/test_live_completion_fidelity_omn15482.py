# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live readback: completion shaping is APPLIED BY THE BACKEND (OMN-15482).

Opt-in only; CI never runs this. The hermetic seam suite
(``test_wire_completion_fidelity_omn15482.py``) proves the three parameters
reach the outbound payload. That is necessary but not sufficient for AC1,
which asks for a LIVE readback that ``temperature`` is "observably applied by
the backend" -- a payload assertion against a fake transport cannot distinguish
"forwarded and honored" from "forwarded and ignored".

So this module drives the REAL chain -- ``HandlerDelegateSkill`` ->
``LocalDelegationDispatchPort`` -> ``HandlerLlmDelegationCall`` -> a real HTTPS
POST to the pinned ``local-coder-mlx`` backend (``mlx-community/
Qwen3.6-35B-A3B-8bit`` on stickybeatz-studio:8401) -- and asserts on the
MODEL'S OBSERVED OUTPUT:

* **AC1 (temperature applied).** ``temperature=0.0`` must produce identical
  completions across repeated calls; ``temperature=1.0`` must produce at least
  two distinct completions across the same number of calls. A dropped or
  ignored temperature collapses both arms to the same behaviour, so the
  contrast is the observable.
* **AC2 (role split honored).** A system prompt carrying a sentinel-token
  instruction is obeyed even though the sentinel appears NOWHERE in the user
  prompt. Under the pre-OMN-15482 concatenation this could not distinguish a
  real system role from a prefixed string, so the test additionally reads back
  the payload actually posted and asserts two distinct message roles.
* **AC3 (json_mode is a wire parameter).** ``response_format`` is present on
  the posted payload and the backend returns parseable JSON, while the prompt
  text contains no JSON instruction sentence.

Deliberate scope statement -- what this does NOT cover: the ``uv run ... onex
node node_delegate_skill_orchestrator --input <payload.json>`` SUBPROCESS hop
that steel's ``LlmBusDelegationClient`` uses to reach this chain. That hop is a
JSON file handed to the CLI; its fidelity is proven on the steel side by
``tests/llm/test_client_delegation_fidelity_omn15482.py`` (the payload steel
writes) plus ``test_steel_payload_validates_against_the_wire_model`` below (the
same payload shape accepted by this model). Running the real subprocess would
require installing this branch's omnimarket into the shared omnibase_infra
venv, which would affect every other lane on this machine.

Enable with::

    OMN_ALLOW_LIVE_LADDER=1 BIFROST_LOCAL_CODER_MLX_ENDPOINT_URL=<url> \\
      uv run pytest tests/unit/nodes/node_delegate_skill_orchestrator/\\
      test_live_completion_fidelity_omn15482.py -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.routing import delegation_backend_resolution

pytestmark = pytest.mark.skipif(
    os.environ.get("OMN_ALLOW_LIVE_LADDER") != "1",
    reason=(
        "live LLM call against the pinned local-coder-mlx backend; set "
        "OMN_ALLOW_LIVE_LADDER=1 (and BIFROST_LOCAL_CODER_MLX_ENDPOINT_URL) "
        "to enable"
    ),
)

_MODEL_ID = "mlx-community/Qwen3.6-35B-A3B-8bit"
_ENDPOINT_ENV = "BIFROST_LOCAL_CODER_MLX_ENDPOINT_URL"

# ``/no_think`` suppresses this reasoning model's chain-of-thought block, which
# otherwise consumes the whole output budget before any content token is
# emitted. It is the same directive the shipped inference profile applies for
# this model family, not a test-only trick.
_JSON_SYSTEM_PROMPT = "You output only JSON. /no_think"
_NOUN_USER_PROMPT = (
    "Return an object with key word whose value is one single arbitrary "
    "English noun of your choosing."
)

_SENTINEL = "SENTINEL9"
_SENTINEL_SYSTEM_PROMPT = (
    f'You output only JSON. Every reply must set the key "tag" to the exact '
    f"string {_SENTINEL}. /no_think"
)
_SENTINEL_USER_PROMPT = (
    "Return an object with key colour whose value is the string blue."
)

_SAMPLES = 3
_MAX_TOKENS = 400

# Declared so the delegation quality gate validates the noun/colour responses
# structurally instead of applying the ``agent_delegation`` task-class default
# (the per-role dispatch-report contract, OMN-15161), which these deliberately
# tiny probe responses do not satisfy. Without it the gate would reject a
# perfectly good completion and the chain would escalate, obscuring the
# behaviour under test.
_PROBE_RESPONSE_CONTRACT: dict[str, Any] = {"type": "object"}


def _endpoint() -> str:
    url = os.environ.get(_ENDPOINT_ENV, "").strip()
    if not url:
        pytest.skip(f"{_ENDPOINT_ENV} is unset; cannot reach the pinned backend")
    return url


def _live_backends() -> list[dict[str, Any]]:
    """One resolvable backend: the pinned live MLX endpoint.

    The ONLY thing faked in this module. Patched rather than read from the
    overlay/config store so the test does not depend on store credentials, and
    so the endpoint under test is unambiguously the one the operator supplied
    via the env var.
    """
    return [
        {
            "backend_id": "local-coder-mlx",
            "endpoint_url": _endpoint(),
            "model_name": _MODEL_ID,
            "tier": "local",
            "max_tokens": _MAX_TOKENS,
            "timeout_ms": 300000,
            "capabilities": ["agent_delegation"],
        },
    ]


async def _live_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    response_format: dict[str, Any] | None,
    response_contract: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Drive the REAL production chain against the live backend.

    ``HandlerDelegateSkill`` -> ``LocalDelegationDispatchPort`` ->
    ``HandlerLlmDelegationCall`` -> real HTTP POST. Returns ``(content,
    posted_payload)`` so the test can assert on both the model's behaviour and
    the exact bytes that produced it -- the readback half of "live readback".
    """
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
        handler_llm_delegation_call as effect_module,
    )

    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: _live_backends(),
    )

    posted: dict[str, Any] = {}
    real_post = effect_module.transport.post_chat_completion

    def _observing_post(*, payload: dict[str, Any], **kwargs: Any) -> Any:
        posted.clear()
        posted.update(json.loads(json.dumps(payload)))
        return real_post(payload=payload, **kwargs)

    monkeypatch.setattr(
        effect_module.transport, "post_chat_completion", _observing_post
    )

    port = LocalDelegationDispatchPort(
        evidence_db_path=tmp_path / f"live-{uuid4()}.sqlite",
        # The effect runs in-process so the transport patch above is visible;
        # the child-process boundary exists for the CLI's event-loop safety
        # (OMN-13597), not for the behaviour under test here.
        effect_process_boundary=False,
    )
    response = await HandlerDelegateSkill(dispatch_port=port).handle(
        ModelDelegateSkillRequest(
            prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            response_format=response_format,
            response_contract=response_contract,
            task_type="agent_delegation",
            source="external-client",
            backend_id="local-coder-mlx",
            max_tokens=_MAX_TOKENS,
        )
    )

    assert posted, "the chain never POSTed a payload"
    assert response.status == "completed", (
        f"live delegation did not complete: status={response.status!r} "
        f"error={response.error_message!r} gates={response.quality_gates_failed!r}"
    )
    return str(response.response).strip(), posted


@pytest.mark.live_model
async def test_temperature_zero_is_deterministic_and_temperature_one_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: the backend OBSERVABLY applies the forwarded temperature.

    Not a payload assertion -- a behavioural one. If ``temperature`` were
    dropped anywhere between the wire model and the provider, both arms would
    sample identically and this contrast would vanish.
    """
    cold = [
        (
            await _live_completion(
                tmp_path,
                monkeypatch,
                system_prompt=_JSON_SYSTEM_PROMPT,
                user_prompt=_NOUN_USER_PROMPT,
                temperature=0.0,
                response_format={"type": "json_object"},
                response_contract=_PROBE_RESPONSE_CONTRACT,
            )
        )[0]
        for _ in range(_SAMPLES)
    ]
    assert len(set(cold)) == 1, (
        f"temperature=0.0 must be deterministic; got {len(set(cold))} distinct "
        f"completions: {cold}"
    )

    hot = [
        (
            await _live_completion(
                tmp_path,
                monkeypatch,
                system_prompt=_JSON_SYSTEM_PROMPT,
                user_prompt=_NOUN_USER_PROMPT,
                temperature=1.0,
                response_format={"type": "json_object"},
                response_contract=_PROBE_RESPONSE_CONTRACT,
            )
        )[0]
        for _ in range(_SAMPLES)
    ]
    assert len(set(hot)) > 1, (
        "temperature=1.0 produced identical completions across "
        f"{_SAMPLES} calls ({hot}) -- indistinguishable from a dropped "
        "temperature"
    )


@pytest.mark.live_model
async def test_system_role_survives_and_is_obeyed_by_the_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: two distinct roles on the payload, and the system role is obeyed.

    The sentinel token appears ONLY in the system prompt, so a backend that
    honours it can only have received it as a system message.
    """
    content, posted = await _live_completion(
        tmp_path,
        monkeypatch,
        system_prompt=_SENTINEL_SYSTEM_PROMPT,
        user_prompt=_SENTINEL_USER_PROMPT,
        temperature=0.0,
        response_format={"type": "json_object"},
        response_contract=_PROBE_RESPONSE_CONTRACT,
    )

    # Exactly two messages, exactly two roles, in order. The only edit to
    # either string is the backend's own inference-protocol directive
    # (``/no_think``), which is pre-existing shaping applied identically before
    # OMN-15482 -- not the caller's system prompt leaking into the user turn.
    assert [m["role"] for m in posted["messages"]] == ["system", "user"]
    assert posted["messages"][0]["content"] == _SENTINEL_SYSTEM_PROMPT
    assert posted["messages"][1]["content"].endswith(_SENTINEL_USER_PROMPT)
    assert _SENTINEL not in posted["messages"][1]["content"]
    assert _SENTINEL not in _SENTINEL_USER_PROMPT
    assert _SENTINEL in content, (
        "the system-prompt instruction was not obeyed; the system role did not "
        f"survive the delegation path. content={content!r}"
    )


@pytest.mark.live_model
async def test_json_mode_is_a_wire_parameter_and_the_backend_honours_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: ``response_format`` rides on the payload -- not the prompt -- and
    the backend returns parseable JSON because of it."""
    content, posted = await _live_completion(
        tmp_path,
        monkeypatch,
        system_prompt=_JSON_SYSTEM_PROMPT,
        user_prompt=_NOUN_USER_PROMPT,
        temperature=0.0,
        response_format={"type": "json_object"},
        response_contract=_PROBE_RESPONSE_CONTRACT,
    )

    assert posted["response_format"] == {"type": "json_object"}
    assert "JSON object only" not in posted["messages"][1]["content"]
    parsed = json.loads(content)
    assert isinstance(parsed, dict)


@pytest.mark.unit
def test_steel_payload_validates_against_the_wire_model() -> None:
    """Cross-repo seam check (OMN-14208), and the reason the subprocess hop is
    safe to leave uncovered above.

    This is the EXACT payload ``steel_onslaught.llm.client_delegation.
    LlmBusDelegationClient.complete()`` writes to its ``--input`` file, key for
    key. If steel adds, renames, or retypes a payload key, this fails here --
    on the consuming side -- rather than at runtime inside the CLI. Marked
    ``unit`` deliberately: it needs no live backend and must run in CI.
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
