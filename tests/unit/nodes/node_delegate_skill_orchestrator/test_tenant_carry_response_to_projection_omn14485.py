# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14485 — tenant-carry cross-boundary regression for the delegate-skill write-path.

The multitenant write-path (OMN-14208 epic / OMN-14349 / OMN-14058) was a LIVE
NO-OP for tenant-carry: a tenant-tagged ``ModelDelegateSkillRequest`` dispatched,
the FSM completed, and ``node_projection_delegation`` wrote a ``delegation_events``
row — but the row's ``tenant_id`` was the ``'omninode'`` column default, never the
injected tenant (live dogfood cid ``cb6f3cb5`` on the .201 stability lane).

The dropped hop is the RESPONSE, not the dispatch port. ``HandlerDelegateSkill``
threaded ``request.tenant_id`` into the dispatch port (OMN-14349, already tested),
but the ``ModelDelegateSkillResponse`` it returned had NO ``tenant_id`` field. The
runtime auto-publishes that response as the ``delegate-skill-completed.v1`` terminal
event, and ``node_projection_delegation`` reads the row's tenant from that terminal.
A tenant-less terminal → the ``'omninode'`` default. Every prior test passed because
none drove the real seam end-to-end: the handler tests asserted the tenant reached
the dispatch PORT; the projection tests constructed a terminal that ALREADY carried
the tenant. This is exactly the CLAUDE.md "two green suites, silent no-op" failure —
so this test drives the ACTUAL seam:

    request.tenant_id=X
      -> HandlerDelegateSkill.handle() -> ModelDelegateSkillResponse
      -> response.model_dump()          (the terminal event payload the runtime emits)
      -> HandlerProjectionDelegation.handle()  (the live projection entry)
      -> delegation_events row['tenant_id'] == X

It goes RED against the pre-fix code (the response drops the tenant → the row omits
it; on real Postgres the omitted key resolves to the ``'omninode'`` default — the
exists-but-WRONG live defect) and GREEN once the response carries the tenant.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.config.settings import Settings
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_delegate_skill as handler_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

# OMN-15683: delegation_events.tenant_id is now UUID, resolved from a closed
# slug->UUID mapping. The original dogfood slug "mt-dogfood-omn14481" (the
# live incident this module documents) has no entry in that mapping and
# would raise UnmappedTenantIdentityError rather than exercising the
# tenant-carry seam this module actually tests -- use a mapped tenant
# instead. The mechanism under test (does the tenant reach the row at all)
# is unaffected by which mapped slug is used.
_INJECTED_TENANT = "beta-business-proof"
_INJECTED_TENANT_UUID = "91c74442-1233-4c97-b191-911a10346fdf"


class _StubDispatchPort:
    """In-process dispatch port returning a completed result (no network).

    Mirrors the shape the runtime dispatch port returns on the delegate-skill path
    so the response is built from a realistic result dict.
    """

    async def dispatch(
        self,
        *,
        prompt: str,
        task_type: str,
        correlation_id: object,
        max_tokens: int | None,
        source_file_path: str | None,
        source_session_id: str | None,
        wait: bool,
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
        tenant_id: str | None,
        backend_id: str | None = None,
        response_contract: dict[str, object] | None = None,
        # OMN-15482: completion-shaping parameters added to
        # ``ProtocolDelegationDispatchPort``. Accepted and ignored here -- this
        # stub proves tenant carry, not completion shaping -- but they must be
        # DECLARED, because the handler now always passes them and a stub that
        # rejects an unexpected keyword surfaces as a plausible-looking
        # ``status="failed"`` response rather than a loud TypeError.
        system_prompt: str | None = None,
        temperature: float | None = None,
        response_format: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "status": "completed",
            "content": "tenant-carry proof",
            "delegated_to": "local-runtime",
            "model_name": "qwen-coder",
            "quality_gate_passed": True,
            "quality_score": 0.95,
        }


async def _drive_write_path(
    request: ModelDelegateSkillRequest,
) -> dict[str, object]:
    """Drive request -> handler response -> serialized terminal -> projection row.

    Returns the single ``delegation_events`` row the projection materialized from
    the terminal event payload the runtime would publish for this response.
    """
    handler = HandlerDelegateSkill(dispatch_port=_StubDispatchPort())
    response = await handler.handle(request)

    # The runtime auto-publishes the handler's typed response as the
    # delegate-skill-completed.v1 terminal event. model_dump(mode="json") is that
    # published payload — the exact seam node_projection_delegation consumes.
    terminal_payload = response.model_dump(mode="json")

    db = InmemoryDatabaseAdapter()
    projection = HandlerProjectionDelegation()
    projection.handle(
        {
            **terminal_payload,
            "_db": db,
            "_event_type": "delegate-skill-completed",
        }
    )
    rows = db.query("delegation_events")
    assert len(rows) == 1, "the terminal must materialize exactly one row"
    return rows[0]


@pytest.mark.unit
async def test_request_tenant_carries_through_response_to_projection_row() -> None:
    """A request-carried tenant_id reaches the delegation_events projection row.

    This is the load-bearing cross-boundary proof. Matches the live dogfood, where
    BOTH request.tenant_id and ONEX_TENANT_ID were the injected tenant.
    """
    request = ModelDelegateSkillRequest(
        prompt="Write a tenant-carry regression",
        task_type="test",
        source="claude-code",
        correlation_id=uuid4(),
        tenant_id=_INJECTED_TENANT,
    )

    row = await _drive_write_path(request)

    # RED before the fix: the response had no tenant_id field, so the terminal
    # payload was tenant-less and the row omitted tenant_id (== 'omninode' default
    # on real Postgres). GREEN after: the response carries the injected tenant.
    # OMN-15683: the projection resolves the verified slug to its canonical UUID
    # before the row is built, so the stored value is the UUID, not the slug.
    assert row.get("tenant_id") == _INJECTED_TENANT_UUID, (
        "delegation_events row must carry the injected tenant's resolved UUID, "
        f"not the default; got {row.get('tenant_id')!r}"
    )
    # Never the interim single-tenant column default masquerading as isolation.
    assert row.get("tenant_id") != "omninode"


@pytest.mark.unit
async def test_response_object_itself_carries_the_tenant() -> None:
    """Pin the exact dropped hop: the response (terminal payload) carries the tenant.

    The tenant was reaching the dispatch port (OMN-14349) but never the response,
    so the auto-published terminal — built from this response — dropped it.
    """
    handler = HandlerDelegateSkill(dispatch_port=_StubDispatchPort())
    request = ModelDelegateSkillRequest(
        prompt="Write a tenant-carry regression",
        task_type="test",
        source="claude-code",
        tenant_id=_INJECTED_TENANT,
    )

    response = await handler.handle(request)

    assert response.tenant_id == _INJECTED_TENANT
    # The serialized terminal event payload must carry the tenant field, field-for-
    # field, so the projection's from_payload reads it (seam match, not two suites).
    assert response.model_dump(mode="json").get("tenant_id") == _INJECTED_TENANT


@pytest.mark.unit
async def test_env_tenant_interim_carries_through_response_to_projection_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ONEX_TENANT_ID interim (OMN-14058) also reaches the projection row.

    The realistic runtime case: the lane sets ONEX_TENANT_ID but the request carries
    no verified tenant_id. The response must fall back to the env-var interim so the
    row still stamps a real tenant instead of the 'omninode' default.
    """
    # OMN-15683: a mapped slug, distinct from _INJECTED_TENANT above, so this
    # test still proves the env-interim path independently -- "mt-env-only-
    # tenant" has no canonical UUID mapping and would raise.
    env_tenant = "beta-gateway-canary-79afa7263852"
    env_tenant_uuid = "79afa726-3852-464f-b7a4-d4b8b9c75ee7"
    monkeypatch.setattr(
        handler_module,
        "get_settings",
        lambda: Settings(onex_tenant_id=env_tenant),
    )

    request = ModelDelegateSkillRequest(
        prompt="Write a tenant-carry regression",
        task_type="test",
        source="claude-code",
        correlation_id=uuid4(),
        # No request-carried tenant: only the env interim resolves the identity.
        tenant_id=None,
    )

    row = await _drive_write_path(request)

    assert row.get("tenant_id") == env_tenant_uuid, (
        "env-var interim tenant's resolved UUID must carry onto the row when the "
        f"request omits one; got {row.get('tenant_id')!r}"
    )


@pytest.mark.unit
async def test_no_tenant_resolves_to_none_and_omits_the_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No verified tenant AND no ONEX_TENANT_ID -> None -> column default applies.

    The fix must not over-reach: with neither source set, the response carries None
    and the projection omits the key so the 'omninode' column default still applies
    (the honest single-tenant fallback), never a spurious stamp.
    """
    monkeypatch.setattr(
        handler_module,
        "get_settings",
        lambda: Settings(onex_tenant_id=""),
    )

    request = ModelDelegateSkillRequest(
        prompt="Write a tenant-carry regression",
        task_type="test",
        source="claude-code",
        correlation_id=uuid4(),
        tenant_id=None,
    )

    row = await _drive_write_path(request)

    # The projection omits the key (InmemoryDatabaseAdapter has no column default),
    # so the value is absent/None here; on real Postgres the DEFAULT 'omninode'
    # applies. Either way, no spurious non-default tenant is stamped.
    assert row.get("tenant_id") in (None, "omninode")
