# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19442: a customer's declared local model answers a two-word prompt.

Measured on 2026-09-24 from a clean install (core 0.47.20, infra 0.38.56,
market 0.4.215), fresh ``HOME``, with one local model declared the way the
no-model refusal's own example says to declare it (``local-coder`` only) and no
provider key. ``onex delegate "say ok"`` was auto-classed ``document`` and
refused with ``ONEX_MARKET_CUSTOMER_PROVIDER_KEY_ABSENT``: "the resolved route
would run on a platform-owned key ... or declare a local model". The customer
HAD declared one.

The mechanism: ``document``'s only local rung is ``local-heavy-reasoning``. With
that rung undeclared, ``first_eligible_tier`` finds no routable tier (the cloud
rungs have no credential), and the untargeted fallback picked the first backend
whose ``use_for`` lists ``document`` -- the house-credentialed OpenRouter rung,
which the customer-key terminus then refused. ``research`` completed on the
same install only by accident: no credentialed backend lists it, so the
fallback's "first backend with any endpoint" landed on ``local-coder``.

These tests run the real shipped routing contracts (routing tiers, task-class
contracts, bifrost contract) with a customer overlay file and no key resolving
on the machine. Only the provider call is replaced, by an effect that records
what it was handed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_infra.errors import ProtocolConfigurationError

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
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from omnimarket.routing import delegation_backend_resolution as resolution_mod
from omnimarket.routing.customer_key_terminus import CustomerKeyRefusedError

pytestmark = pytest.mark.unit

_CUSTOMER = str(uuid4())
_SERVED_MODEL = "customer-local-model"
_LOOPBACK = (
    "http://127.0.0.1:18742/v1/chat/completions"  # url-authority-ok: test loopback
)

# A gate-passing ``document`` answer, so these proofs stay about routing. The
# single-word verdict on a bare "ok" is plan slice 1 (OMN-13967), not this one.
_GOOD_DOCUMENT = (
    "### ANSWER\n"
    "The sky is blue because Rayleigh scattering by air molecules scatters "
    "short blue wavelengths of sunlight far more strongly than red ones."
)


class _RecordingEffect:
    """Stands in for the provider call and records every request it is handed."""

    def __init__(self) -> None:
        self.calls: list[ModelLlmDelegationCallRequest] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_GOOD_DOCUMENT,
            tokens_in=12,
            tokens_out=30,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )


def _overlay(path: Path, backend_ids: tuple[str, ...]) -> Path:
    lines = ["backends:"]
    for backend_id in backend_ids:
        lines += [
            f"  - backend_id: {backend_id}",
            f'    endpoint_url: "{_LOOPBACK}"',
            f'    model_name: "{_SERVED_MODEL}"',
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def clean_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """The shipped contracts, the customer's overlay file, and no key anywhere.

    Yields the overlay path; each test writes the declaration it needs. The
    overlay is bound through ``BIFROST_OVERLAY_PATH`` because that is the one
    seam both the routing authority and the port's resolver read (OMN-18676);
    ``_OVERLAY_PATH`` is pointed at the same file so a refusal names it.
    """
    overlay = tmp_path / "home" / ".omninode" / "delegation" / "bifrost_overrides.yaml"
    overlay.parent.mkdir(parents=True)
    for key in (
        "BIFROST_CONTRACT_PATH",
        "DELEGATION_ROUTING_TIERS_PATH",
        "TASK_CLASS_CONTRACT_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))
    monkeypatch.setattr(resolution_mod, "_OVERLAY_PATH", overlay)
    # No provider key resolves on this machine: not the house's, not theirs.
    # A backend that declares no credential stays available, as it is in
    # ``api_key_ref_available`` itself.
    monkeypatch.setattr(
        routing,
        "api_key_ref_available",
        lambda api_key_ref, **_k: not api_key_ref,
    )
    routing._load_bifrost_endpoints.cache_clear()
    yield overlay
    routing._load_bifrost_endpoints.cache_clear()


def _dispatch(
    tmp_path: Path, effect: _RecordingEffect, *, tenant_id: str, prompt: str
) -> dict[str, object]:
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "evidence.sqlite",
        effect_process_boundary=False,
    )
    return asyncio.run(
        port.dispatch(
            prompt=prompt,
            task_type="document",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=tenant_id,
        )
    )


def test_one_declared_local_model_answers_a_two_word_document_prompt(
    clean_install: Path, tmp_path: Path
) -> None:
    """AC1: the declared model serves ``document``; nothing is refused."""
    _overlay(clean_install, ("local-coder",))
    effect = _RecordingEffect()

    try:
        result = _dispatch(tmp_path, effect, tenant_id=_CUSTOMER, prompt="say ok")
    except CustomerKeyRefusedError as exc:
        pytest.fail(
            "a customer with a declared local model and no key was refused: "
            f"{exc.refusal.message}"
        )

    assert result["status"] == "completed", result.get("error_message")
    assert [call.endpoint_ref for call in effect.calls] == [_LOOPBACK]
    assert [call.model_id for call in effect.calls] == [_SERVED_MODEL]
    assert all(call.secret_ref is None for call in effect.calls)
    assert result["model_name"] == _SERVED_MODEL


def test_the_class_rung_is_still_preferred_when_it_is_declared(
    clean_install: Path, tmp_path: Path
) -> None:
    """Control: with both local rungs declared, ``document`` keeps its own rung."""
    _overlay(clean_install, ("local-coder", "local-heavy-reasoning"))
    effect = _RecordingEffect()

    result = _dispatch(tmp_path, effect, tenant_id=_CUSTOMER, prompt="say ok")

    assert result["status"] == "completed", result.get("error_message")
    attempts = result["attempts"]
    assert isinstance(attempts, list)
    assert [attempt["backend_id"] for attempt in attempts] == ["local-heavy-reasoning"]


def test_house_work_is_not_rerouted(clean_install: Path, tmp_path: Path) -> None:
    """Control: the substitution is for customer work only.

    House work keeps the untargeted resolution unchanged (OMN-15630 forbids
    binding a class to an off-capability rung by accident), so with the class
    rung undeclared it is NOT moved onto ``local-coder``.
    """
    _overlay(clean_install, ("local-coder",))
    effect = _RecordingEffect()

    _dispatch(tmp_path, effect, tenant_id=HOUSE_TENANT_SLUG, prompt="say ok")

    assert effect.calls, "house work reached no backend at all"
    assert effect.calls[0].endpoint_ref != _LOOPBACK


def test_no_model_and_no_key_names_both_remedies(
    clean_install: Path, tmp_path: Path
) -> None:
    """AC2: the positive control. Nothing declared, nothing registered."""
    clean_install.write_text("backends: []\n", encoding="utf-8")
    effect = _RecordingEffect()

    with pytest.raises(ProtocolConfigurationError) as exc_info:
        _dispatch(tmp_path, effect, tenant_id=_CUSTOMER, prompt="say ok")

    message = str(exc_info.value)
    assert "ONEX_CORE_041_INVALID_CONFIGURATION" in message
    assert "No local model is declared" in message
    assert str(clean_install) in message
    assert "register your own provider key" in message
    assert effect.calls == []
