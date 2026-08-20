# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""End-to-end regression: a broken/mismatched venv never silently reaches paid cloud (OMN-14097).

Root cause (2026-07-07 dogfood, previously falsely marked Done and reverted the
same day 2026-07-15): running ``onex delegate`` from omnimarket's own venv hit a
stale-dependency import gap (the exact repro is now pinned at the dependency
level under OMN-14003) and the delegation silently escalated to a paid cloud
tier with a fully "successful" (``exit 0``) result — spending real money for a
bug that had nothing to do with the local endpoint's actual reachability.

``transport._httpx_probe_health``'s bare ``except Exception: return False``
(fixed alongside this test) was the architectural hole: it could not
distinguish "genuinely unreachable" from "this process's code/environment is
broken," so BOTH silently skipped the local (free) tier the same way. These
tests drive the exact end-to-end path a broken venv exercises — the health
probe deep inside the canonical effect handler raising a programming-defect
exception (``ImportError``, standing in for the stale ``omnibase-spi`` symptom)
— and assert two invariants that together close the ticket:

1. ``LocalDelegationDispatchPort.dispatch`` never returns ``status="completed"``
   with a paid/cloud provider when the failure is this class of exception; it
   propagates instead of being silently absorbed as "local unhealthy, use cloud".
2. The outer ``HandlerDelegateSkill.handle`` (the real consumer-facing surface
   ``onex delegate`` calls) converts that propagation into an explicit
   ``status="failed"`` response with ``cost_usd == 0.0`` and an empty/local
   provider — never a silent paid completion.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
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
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call,
)
from omnimarket.routing import delegation_backend_resolution

_BROKEN_VENV_MESSAGE = (
    "cannot import name 'ProtocolDispatchEngine' from 'omnibase_spi.protocols.runtime'"
)


@pytest.fixture(autouse=True)
def _clear_health_cache() -> None:
    """See test_local_dispatch_evidence.py — module-level probe cache guard."""
    handler_llm_delegation_call._health_cache.clear()


@pytest.fixture
def fake_local_backend() -> list[dict[str, object]]:
    return [
        {
            "backend_id": "local-coder",
            "endpoint_url": "http://100.109.203.94:8000/v1/chat/completions",  # onex-allow-test-fixture OMN-16156 reason="real local-inference endpoint used as fixture data for the local backend under test"
            "model_name": "Qwen3.6-35B-A3B",
            "tier": "local",
            "max_tokens": 65536,
            "timeout_ms": 300000,
            "capabilities": ["research"],
        }
    ]


def _patch_routing(
    monkeypatch: pytest.MonkeyPatch, backends: list[dict[str, object]]
) -> None:
    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: backends,
    )


def _patch_probe_raises_broken_venv_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the stale-venv symptom deep inside the REAL probe transport.

    Patches ``httpx.Client`` (not ``transport.probe_health`` itself) so the
    call drives the actual production chain —
    ``HandlerLlmDelegationCall._is_endpoint_healthy`` ->
    ``transport.probe_health`` -> ``transport._httpx_probe_health`` ->
    ``httpx.Client(...).get(...)`` — and hits the exact code this ticket's fix
    changes. Raising here at the httpx-client boundary (rather than stubbing
    ``transport.probe_health`` wholesale) is what actually exercises the bare
    ``except Exception`` this ticket closes; stubbing the outer function would
    bypass it entirely and prove nothing about the real defect.
    """

    class _ExplodingClient:
        def __enter__(self) -> _ExplodingClient:
            return self

        def __exit__(self, *_exc_info: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> object:
            raise ImportError(_BROKEN_VENV_MESSAGE)

    monkeypatch.setattr(httpx, "Client", lambda *_args, **_kwargs: _ExplodingClient())


@pytest.mark.unit
async def test_local_dispatch_port_propagates_broken_venv_exception_never_completes_paid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_local_backend: list[dict[str, object]],
) -> None:
    """The port must propagate a broken-venv-class exception, never complete paid.

    Before the fix this silently resolved the local tier "unhealthy" and
    escalated (successfully) to the next paid tier. After the fix, the
    exception is not a reachability signal — it propagates out of dispatch()
    rather than being absorbed into a "completed" result.
    """
    db_path = tmp_path / "delegation.sqlite"
    _patch_routing(monkeypatch, fake_local_backend)
    _patch_probe_raises_broken_venv_exception(monkeypatch)

    port = LocalDelegationDispatchPort(
        evidence_db_path=db_path,
        effect_process_boundary=False,
    )
    with pytest.raises(ImportError, match="ProtocolDispatchEngine"):
        await port.dispatch(
            prompt="explain the routing architecture",
            task_type="research",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )


@pytest.mark.unit
async def test_handler_delegate_skill_fails_loud_never_silently_pays_on_broken_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_local_backend: list[dict[str, object]],
) -> None:
    """The consumer-facing handler (what ``onex delegate`` calls) fails loud.

    This is the OMN-14097 acceptance bar end to end: the real ``onex delegate``
    consumer surface (``HandlerDelegateSkill.handle``, which
    ``LocalDelegationDispatchPort.dispatch`` feeds via
    ``select_delegation_dispatch_port`` in production) must return
    ``status="failed"`` with ``cost_usd == 0.0`` and no cloud/paid provider
    stamped — never a silent ``status="completed"`` cloud escalation.
    """
    db_path = tmp_path / "delegation.sqlite"
    _patch_routing(monkeypatch, fake_local_backend)
    _patch_probe_raises_broken_venv_exception(monkeypatch)

    port = LocalDelegationDispatchPort(
        evidence_db_path=db_path,
        effect_process_boundary=False,
    )
    handler = HandlerDelegateSkill(dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="explain the routing architecture",
        task_type="research",
        source="claude-code",
    )

    response = await handler.handle(request)

    assert response.status == "failed"
    assert response.metrics.cost_usd == 0.0
    assert "ProtocolDispatchEngine" in response.error_message
    # No paid/cloud provider was ever reached — provider stays empty, not
    # stamped with a cloud endpoint or model id.
    assert response.provider == ""
    assert response.model_name == ""
