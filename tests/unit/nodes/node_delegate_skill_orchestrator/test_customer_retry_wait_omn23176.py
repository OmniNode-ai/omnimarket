# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A customer route waits between its same-route re-issues (OMN-23176).

C29 run 42639763864 (2026-11-09T14:57Z): the customer's OpenRouter model answered
429 "temporarily rate-limited upstream. Please retry shortly", the port re-issued
the route twice, and all three calls were made inside 5.5 s, so the whole retry
budget was spent in one throttle window and the delegation failed (the same on
runs 42589695452 and 42564608889). The catalogue now declares ``retry_wait_seconds``
for the row and the port waits that long before each re-issue, only while the run
budget still holds the wait and another attempt.

Drives the customer route through the local dispatch port as production wires it;
the provider is a double that always throttles, and the wait is an injected sleeper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.handlers.routing import byok_provider_backends as catalogue
from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.inference.local_byok_credential_adapter import (
    register_local_byok_credential,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallResult,
)
from tests.test_omn20157_byok_model_discovery_and_typed_refusals import (
    _isolated_effects,
    _row,
)
from tests.unit.nodes.node_delegate_skill_orchestrator.test_local_dispatch_escalation_omn13849 import (
    _install_ladder,
)

pytestmark = [pytest.mark.unit]
_SHARED_AUTOUSE_FIXTURE = _isolated_effects

KEY = "synthetic-retry-wait-key-asserted-absent-from-outputs"
MODEL = "google/gemini-2.5-flash-lite"
THROTTLE = f"{MODEL} is temporarily rate-limited upstream. Please retry shortly"


@dataclass
class _AlwaysThrottled:
    """The provider double: every call answers the 429 the C29 run read."""

    calls: list[str] = field(default_factory=list)

    def __call__(self, request: Any) -> ModelLlmDelegationCallResult:
        self.calls.append(request.model_id)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=False,
            failure_class="rate_limited",
            error_message=THROTTLE,
            http_status=429,
            endpoint_healthy=True,
            secret_source=EnumSecretSource.LOCAL_STORE,
            secret_ref=request.secret_ref,
        )


def _customer_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    customer_ref = register_local_byok_credential(
        "openrouter", KEY, model=MODEL, model_chosen_by="customer", db_path=db_path
    )
    _install_ladder(monkeypatch, max_escalations=0)
    backend = ModelResolvedDelegationBackend(
        backend_id="byok-openrouter",
        model_id=MODEL,
        endpoint_ref=str(_row("openrouter").endpoint_url),
        tier="cheap_cloud",
        max_tokens=256,
        timeout_ms=60000,
        secret_ref=customer_ref,
    )
    monkeypatch.setattr(
        port_mod, "resolve_delegation_backend", lambda *_args, **_kwargs: backend
    )
    # The stubbed budgets here are small; the real classes declare a 60 s reserve.
    monkeypatch.setattr(port_mod, "_next_rung_reserve_seconds", lambda _task_type: 0.0)
    return db_path


async def _dispatch(
    db_path: Path, provider: _AlwaysThrottled, waits: list[float], *, budget: int
) -> dict[str, object]:
    async def sleeper(seconds: float) -> None:
        waits.append(seconds)

    port = port_mod.LocalDelegationDispatchPort(
        effect_handler=provider,
        evidence_db_path=db_path,
        effect_process_boundary=False,
        # A fixed run-budget clock: the budget never shrinks while the double answers.
        clock=lambda: 0.0,
        sleep=sleeper,
    )
    return await port.dispatch(
        prompt="explain what a calendar app needs",
        task_type="research",
        correlation_id=uuid4(),
        max_tokens=256,
        source_file_path=None,
        source_session_id=None,
        wait=True,
        execution_timeout_seconds=budget,
        terminal_delivery_margin_seconds=1,
        quality_contract_mode="extend_task_class",
        acceptance_criteria=(),
        tenant_id=None,
    )


def test_r1_throttle_retry_the_openrouter_row_declares_a_wait_between_its_reissues() -> (
    None
):
    row = catalogue.resolve_byok_backend_by_id("byok-openrouter")
    assert row is not None
    assert row.max_retries == 2
    assert row.retry_wait_seconds > 0
    assert catalogue.byok_backend_retry_wait_seconds("byok-openrouter") == (
        row.retry_wait_seconds
    )
    # Fail-closed: a ref the catalogue does not declare waits for nothing.
    assert catalogue.byok_backend_retry_wait_seconds("not-a-backend") == 0.0
    assert catalogue.byok_backend_retry_wait_seconds(None) == 0.0


@pytest.mark.asyncio
async def test_r1_throttle_retry_each_reissue_of_a_throttled_customer_route_waits_the_declared_seconds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = _customer_route(monkeypatch, tmp_path)
    provider, waits = _AlwaysThrottled(), []
    await _dispatch(db_path, provider, waits, budget=240)
    declared = catalogue.byok_backend_retry_wait_seconds("byok-openrouter")
    assert provider.calls == [MODEL] * 3  # the first call and the two re-issues
    assert waits == [declared, declared]


@pytest.mark.asyncio
async def test_r1_throttle_retry_control_with_no_declared_wait_the_reissues_go_out_back_to_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The injected break: the accessor the fix reads answers zero, and the run is the C29 one."""
    db_path = _customer_route(monkeypatch, tmp_path)
    monkeypatch.setattr(port_mod, "byok_backend_retry_wait_seconds", lambda _ref: 0.0)
    provider, waits = _AlwaysThrottled(), []
    await _dispatch(db_path, provider, waits, budget=240)
    assert provider.calls == [MODEL] * 3
    assert waits == []  # no wait between the calls: the shape of run 42639763864


@pytest.mark.asyncio
async def test_r1_throttle_retry_a_wait_the_run_budget_cannot_hold_issues_no_reissue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = _customer_route(monkeypatch, tmp_path)
    provider, waits = _AlwaysThrottled(), []
    # A budget shorter than the declared wait: the re-issue would end past the run.
    await _dispatch(db_path, provider, waits, budget=10)
    assert waits == []
    assert provider.calls == [MODEL]
