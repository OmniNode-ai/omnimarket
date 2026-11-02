# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22179: a local model that setup stored and tested counts as the customer's own.

The clean-Mac onboarding walk of 2026-11-02 stored a Local URL through
``onex models add local`` (its model test passed) and the next ``onex delegate``
was refused as ``ONEX_MARKET_CUSTOMER_PROVIDER_KEY_ABSENT`` on
``surface=customer_local``. These tests declare the model through the same call
setup makes (``models add local --url --model``, so the store the command
writes, ``bifrost_overrides.yaml``) and delegate on the minted local customer
tenant with no provider key anywhere.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from click.testing import CliRunner
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.cli.cli_secret import models_group
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.handlers.routing import delegation_backend_resolution as resolution_mod
from omnimarket.handlers.routing.customer_key_terminus import (
    CUSTOMER_PROVIDER_KEY_ABSENT_ONEX_CODE,
    CustomerKeyRefusedError,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from tests.unit.cli.test_model_setup_local_url_omn21162 import (
    _Delegations,
    delegations,
    server,
    state,
)

pytestmark = pytest.mark.unit

__all__ = ["delegations", "server", "state"]

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "onboarding_walk_2026_11_02"
    / "delegate_refusal.json"
)
_MODEL = "qwen3-coder-30b"
#: What a keyless server answers per class; ``code_generation`` is graded on code.
_ANSWERS = {
    "code_generation": "### ANSWER\n```python\ndef hello() -> str:\n    return 'hello'\n```",
}
_ANSWER = "hello"
#: The task classes the router auto-classes. The contract names them document,
#: code_generation, test and planning; the ticket spells two of them "code" and "plan".
_AUTO_CLASSED = ("document", "code_generation", "test", "planning")


class _RecordingEffect:
    def __init__(self, content: str = _ANSWER) -> None:
        self.calls: list[ModelLlmDelegationCallRequest] = []
        self.content = content

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=self.content,
            tokens_in=5,
            tokens_out=1,
            latency_ms=3,
        )


class _FailingEffect(_RecordingEffect):
    """The declared local model's call fails, as a slow or unwell server's does."""

    def __init__(self, *, transport: bool) -> None:
        super().__init__()
        self.transport = transport

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request)
        if self.transport:
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                content="",
                tokens_in=0,
                tokens_out=0,
                failure_class=EnumDelegationFailureClass.MODEL_UNAVAILABLE,
                http_status=503,
                error_message="local server unavailable",
            )
        # The call succeeds and the answer is empty: the quality gate rejects it.
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content="",
            tokens_in=5,
            tokens_out=0,
            latency_ms=3,
        )


@pytest.fixture
def no_key_machine(monkeypatch: pytest.MonkeyPatch, state: Path) -> Iterator[Path]:
    """The shipped contracts, the store ``add local`` writes, and no key anywhere."""
    overlay = state / "bifrost_overrides.yaml"
    for key in (
        "BIFROST_CONTRACT_PATH",
        "DELEGATION_ROUTING_TIERS_PATH",
        "TASK_CLASS_CONTRACT_PATH",
        "ONEX_TENANT_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    # The one seam the routing authority and the dispatch resolver both read.
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    monkeypatch.setattr(resolution_mod, "_OVERLAY_PATH", overlay)
    monkeypatch.setattr(
        routing, "api_key_ref_available", lambda api_key_ref, **_k: not api_key_ref
    )
    routing._load_bifrost_endpoints.cache_clear()
    yield overlay
    routing._load_bifrost_endpoints.cache_clear()


def _add_local(server_url: str) -> None:
    done = CliRunner().invoke(
        models_group, ["add", "local", "--url", server_url, "--model", _MODEL]
    )
    assert done.exit_code == 0, done.output


def _delegate(
    tmp_path: Path, effect: _RecordingEffect, task_type: str
) -> dict[str, Any]:
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )
    # No tenant is passed: on the local path the machine's minted deployment
    # tenant is the customer, exactly as ``onex delegate`` runs it.
    return asyncio.run(
        port.dispatch(
            prompt="Reply with exactly one word: hello",
            task_type=task_type,
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )


@pytest.mark.parametrize("task_type", _AUTO_CLASSED)
def test_a_local_model_declared_by_add_local_is_the_customers_own(
    task_type: str,
    server: str,
    delegations: _Delegations,
    no_key_machine: Path,
    tmp_path: Path,
) -> None:
    """AC1 + AC2: declared through ``models add local``, served for each class."""
    delegations.answer_model = _MODEL
    _add_local(server)
    assert delegations.pinned == ["local-url"], "setup's own pinned test ran"
    declared = yaml.safe_load(no_key_machine.read_text("utf-8"))["backends"]
    assert [b["backend_id"] for b in declared] == ["local-url"]
    effect = _RecordingEffect(_ANSWERS.get(task_type, _ANSWER))

    try:
        result = _delegate(tmp_path, effect, task_type)
    except CustomerKeyRefusedError as exc:
        pytest.fail(f"declared local model refused: {exc.refusal.boundary_message}")

    assert [c.model_id for c in effect.calls][:1] == [_MODEL]
    assert all(c.secret_ref is None for c in effect.calls)
    attempts = result["attempts"]
    assert isinstance(attempts, list)
    assert attempts[0]["backend_id"] == "local-url"
    assert result["status"] == "completed", result.get("quality_gates_failed")


@pytest.mark.parametrize("task_type", _AUTO_CLASSED)
@pytest.mark.parametrize("transport", [True, False], ids=["http_503", "gate_reject"])
def test_walk_2026_11_02_a_declared_local_model_that_fails_is_not_a_missing_key(
    task_type: str,
    transport: bool,
    server: str,
    delegations: _Delegations,
    no_key_machine: Path,
    tmp_path: Path,
) -> None:
    """AC4: the walk's refusal, reproduced and then not recurring.

    The declared model is tried first. When its call fails or its answer is
    rejected, the run used to fall through to the ladder's entry rung, a
    platform-owned key the customer holds none for, and the customer-key
    terminus raised the recorded refusal over the local model's own failure:
    no result, so no attempt and a receipt reading "no backend was reached".
    The run now ends on the local model's failure, naming it.
    """
    recorded = json.loads(_FIXTURE.read_text("utf-8"))
    assert CUSTOMER_PROVIDER_KEY_ABSENT_ONEX_CODE in recorded["refusal"]
    assert recorded["setup_choice"] == "Local URL"
    delegations.answer_model = _MODEL
    _add_local(server)
    effect = _FailingEffect(transport=transport)

    try:
        result = _delegate(tmp_path, effect, task_type)
    except CustomerKeyRefusedError as exc:
        pytest.fail(
            "the walk's refusal recurred over a declared local model: "
            f"{exc.refusal.boundary_message} (recorded surface "
            f"{recorded['surface']}: {recorded['refusal']})"
        )

    assert result["status"] == "failed"
    attempts = result["attempts"]
    assert isinstance(attempts, list)
    # The declared model's own retries: no attempt was made on any other rung.
    assert {a["backend_id"] for a in attempts} == {"local-url"}
    assert all(c.secret_ref is None for c in effect.calls)


def test_walk_2026_11_02_recorded_refusal_names_the_surface_and_code() -> None:
    """The fixture is the walk's own text: the code and surface the tests key on."""
    recorded = json.loads(_FIXTURE.read_text("utf-8"))
    assert recorded["task_type"] == "document"
    assert f"[{CUSTOMER_PROVIDER_KEY_ABSENT_ONEX_CODE}]" in recorded["refusal"]
    assert "surface=customer_local" in recorded["refusal"]


@pytest.mark.parametrize("task_type", _AUTO_CLASSED)
def test_nothing_declared_and_no_key_is_still_refused(
    task_type: str, no_key_machine: Path, tmp_path: Path
) -> None:
    """AC3: the positive control. An empty store reaches no platform-owned key."""
    no_key_machine.write_text("backends: []\n", encoding="utf-8")
    effect = _RecordingEffect()

    with pytest.raises((CustomerKeyRefusedError, ProtocolConfigurationError)) as exc:
        _delegate(tmp_path, effect, task_type)

    assert effect.calls == [], "a route reached a backend with nothing declared"
    message = str(exc.value)
    assert (
        CUSTOMER_PROVIDER_KEY_ABSENT_ONEX_CODE in message
        or "No local model is declared" in message
    )
