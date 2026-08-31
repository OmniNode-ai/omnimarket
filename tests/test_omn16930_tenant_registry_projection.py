# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16930: the tenant-registry projection writer.

What this file pins is narrower than "the projection works": it pins the
properties that make the mirror safe to RESOLVE A MIGRATION AGAINST. A
projection whose rows are merely eventually-correct is fine for a dashboard;
this one is the identity authority an ``ALTER COLUMN ... TYPE UUID USING``
clause joins, so a wrong or missing row does not surface here -- it surfaces
days later as an aborted deploy in a different repo.

The three properties, and why each is a raise rather than a log-and-continue:

* a slug already bound to one UUID may never be rebound to another, because
  every conversion that already resolved through that slug would retroactively
  point at a different tenant (the OMN-15683 cross-tenant reassignment class);
* an envelope that cannot yield an identity is refused at the parse boundary,
  not skipped, so the blame stays local to the malformed message;
* an upsert that returns no row is a failure, not a success, because reporting
  success commits the consumer offset and loses the tenant permanently.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_projection_tenant_registry.handlers.handler_tenant_registry_projection import (
    HandlerTenantRegistryProjectionRunner,
    TenantIdentityRebindingError,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_registry_events import (
    ModelTenantRegistryEvent,
    ModelTenantRegistryRecord,
    TenantRegistryEventError,
)

pytestmark = pytest.mark.unit

# A SYNTHETIC stand-in for the externally-owned tenant whose delegation row
# landed 31 minutes after signup -- the row that made OMN-16804 urgent.
#
# The real slug and registry UUID used to be pinned here as literals. They were
# a live customer's identifiers in a PUBLIC repo, and OMN-17288 replaced them.
# The pair below is provably not anyone's: the UUID is
# uuid5(NAMESPACE_DNS, "t-external-fixture-omn17288.example.invalid"), and
# .invalid is reserved by RFC 2606 precisely so it can never be delegated.
#
# Nothing here asserts a property that needs the real customer's identity --
# what is under test is the resolution MECHANISM. Still pinned as literals
# rather than imported from tenant_isolation.py: this file is the independent
# check on that map, not a restatement of it.
_LIVE_SLUG = "t-external-fixture-omn17288"
_LIVE_UUID = UUID("7527359e-3c87-53fd-a0ae-09fb9c2fe82d")


def _envelope(
    *,
    operation: str = "TENANT_CREATED",
    tenant_slug: str = _LIVE_SLUG,
    tenant_id: str = str(_LIVE_UUID),
    status: str = "active",
    **extra_tenant: Any,
) -> dict[str, Any]:
    """A ``onex.tenant.events`` message shaped exactly like the one onex-api
    enqueues (``main.py`` -> ``enqueue_tenant_event`` -> ``payload.tenant``)."""
    tenant: dict[str, Any] = {
        "tenant_id": tenant_id,
        "tenant_slug": tenant_slug,
        "name": tenant_slug,
        "status": status,
        "created_at": "2026-08-26T16:17:00+00:00",
        "plan_code": "beta",
    }
    tenant.update(extra_tenant)
    return {
        "operation": operation,
        "success": True,
        "correlation_id": "omn16930-test",
        # Transport fields this projection has no interest in. They are here
        # deliberately: the event model is extra="ignore" at the envelope
        # level precisely so routine envelope evolution cannot break the
        # projection, and this asserts that posture rather than assuming it.
        "schema_version": "1.0.0",
        "metadata": {"tags": {"category": "tenant", "event": operation}},
        "payload": {"tenant": tenant},
    }


class _FakeDb:
    """Records statements and replays scripted results.

    Not a stand-in for Postgres -- the real SQL is exercised against a real
    database in ``test_omn16930_conversion_replay.py``. This one exists to
    drive the writer's REFUSAL branches, which are unreachable from a
    happy-path integration test.
    """

    def __init__(self, results: list[list[dict[str, Any]]] | None = None) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self._results = results or []

    async def execute(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.statements.append((sql, args))
        if self._results:
            return self._results.pop(0)
        return []


def _runner(db: _FakeDb) -> HandlerTenantRegistryProjectionRunner:
    runner = HandlerTenantRegistryProjectionRunner()
    runner._db = db  # type: ignore[assignment]
    return runner


def _meta() -> MessageMeta:
    return MessageMeta(
        partition=0, offset=1, fallback_id="omn16930", topic="onex.tenant.events"
    )


class TestEventModel:
    def test_parses_the_real_outbox_payload_shape(self) -> None:
        event = ModelTenantRegistryEvent.from_envelope(_envelope())
        assert event.operation == "TENANT_CREATED"
        assert event.tenant.tenant_slug == _LIVE_SLUG
        assert event.tenant.tenant_id == _LIVE_UUID
        assert event.tenant.status == "active"

    def test_envelope_without_a_tenant_object_raises(self) -> None:
        envelope = _envelope()
        envelope["payload"] = {}
        with pytest.raises(TenantRegistryEventError, match=r"payload\.tenant"):
            ModelTenantRegistryEvent.from_envelope(envelope)

    def test_envelope_without_a_tenant_id_raises_rather_than_defaulting(self) -> None:
        envelope = _envelope()
        del envelope["payload"]["tenant"]["tenant_id"]
        with pytest.raises(TenantRegistryEventError, match="usable registry identity"):
            ModelTenantRegistryEvent.from_envelope(envelope)

    def test_an_unknown_key_inside_the_tenant_object_is_refused(self) -> None:
        """extra="forbid" on the identity itself.

        An unrecognised key in ``payload.tenant`` means the producer changed
        the tenant shape without this consumer being updated. Mirroring it
        anyway would persist an identity built from a shape nobody checked.
        """
        with pytest.raises(TenantRegistryEventError):
            ModelTenantRegistryEvent.from_envelope(_envelope(region_hint="us-east-1"))

    def test_a_padded_slug_is_refused_not_stripped(self) -> None:
        """The slug is matched byte-for-byte against a stored tenant_id.

        Stripping would invent a binding the producer did not send; a padded
        slug would produce a mirror row that silently never joins.
        """
        with pytest.raises(TenantRegistryEventError, match="whitespace"):
            ModelTenantRegistryEvent.from_envelope(
                _envelope(tenant_slug=f" {_LIVE_SLUG} ")
            )

    def test_the_record_is_frozen(self) -> None:
        record = ModelTenantRegistryRecord(
            tenant_id=_LIVE_UUID, tenant_slug=_LIVE_SLUG, status="active"
        )
        with pytest.raises(ValidationError, match="frozen"):
            record.tenant_slug = "other"  # type: ignore[misc]


class TestProjectionWriter:
    async def test_tenant_created_upserts_mirror_row(self) -> None:
        db = _FakeDb(
            results=[
                [],  # the rebinding pre-check: slug not yet mirrored
                [{"tenant_slug": _LIVE_SLUG, "tenant_uuid": str(_LIVE_UUID)}],
            ]
        )
        assert await _runner(db).project_event(
            "onex.tenant.events", _envelope(), _meta()
        )

        upsert_sql, upsert_args = db.statements[1]
        assert "INSERT INTO tenant_registry_mirror" in upsert_sql
        assert "ON CONFLICT (tenant_slug) DO UPDATE" in upsert_sql
        assert upsert_args[0] == _LIVE_SLUG
        assert upsert_args[1] == str(_LIVE_UUID)
        assert upsert_args[3] == "active"

    async def test_registry_created_at_is_never_overwritten_by_a_later_event(
        self,
    ) -> None:
        """COALESCE, not EXCLUDED.

        ``registry_created_at`` is when the tenant was provisioned, not when
        this projection last saw it -- ``observed_at`` is the latter. A
        TENANT_UPDATED replay must not rewrite provisioning history.
        """
        db = _FakeDb(results=[[], [{"tenant_slug": _LIVE_SLUG}]])
        await _runner(db).project_event("onex.tenant.events", _envelope(), _meta())
        upsert_sql, _ = db.statements[1]
        assert "registry_created_at = COALESCE(" in upsert_sql
        assert "observed_at = NOW()" in upsert_sql

    async def test_slug_rebinding_to_a_different_uuid_is_refused(self) -> None:
        """The property that makes the mirror safe to resolve a migration against.

        If a slug could be rebound, every already-converted row that resolved
        through it would retroactively belong to a different tenant. Refused
        BEFORE the upsert, so zero rows change.
        """
        db = _FakeDb(
            results=[[{"tenant_uuid": "91c74442-1233-4c97-b191-911a10346fdf"}]]
        )
        with pytest.raises(TenantIdentityRebindingError, match="Refusing to rebind"):
            await _runner(db).project_event("onex.tenant.events", _envelope(), _meta())
        assert len(db.statements) == 1, "the upsert must not have been attempted"

    async def test_repeat_event_for_the_same_binding_is_accepted(self) -> None:
        db = _FakeDb(
            results=[[{"tenant_uuid": str(_LIVE_UUID)}], [{"tenant_slug": _LIVE_SLUG}]]
        )
        assert await _runner(db).project_event(
            "onex.tenant.events", _envelope(operation="TENANT_UPDATED"), _meta()
        )

    async def test_envelope_without_a_resolvable_tenant_identity_raises(self) -> None:
        envelope = _envelope()
        envelope["payload"] = {"tenant": {"tenant_slug": _LIVE_SLUG}}
        db = _FakeDb()
        with pytest.raises(TenantRegistryEventError):
            await _runner(db).project_event("onex.tenant.events", envelope, _meta())
        assert db.statements == [], "nothing may be written from an unusable envelope"

    async def test_a_non_registry_operation_is_ignored_not_mis_parsed(self) -> None:
        """onex.tenant.events is a shared control-plane topic.

        Declining an operation this node does not own is correct and is NOT
        the same as declining a malformed registry event -- the former returns
        True and writes nothing, the latter raises.
        """
        db = _FakeDb()
        assert await _runner(db).project_event(
            "onex.tenant.events", _envelope(operation="TENANT_BILLING_SYNCED"), _meta()
        )
        assert db.statements == []

    async def test_an_upsert_returning_no_row_is_a_failure_not_a_success(self) -> None:
        """Reporting success would commit the offset and lose the tenant."""
        db = _FakeDb(results=[[], []])
        with pytest.raises(RuntimeError, match="returned no row"):
            await _runner(db).project_event("onex.tenant.events", _envelope(), _meta())


class TestDefBHandlerShape:
    def test_handle_takes_request_and_returns_a_plain_result(self) -> None:
        """OMN-14355 canon-shape ratchet.

        The single positional parameter must be named ``request`` for the
        shared runtime_local_adapter to adapt it, and the handler must not
        traffic in ``ModelEventEnvelope``/``ModelHandlerOutput`` -- the
        pre-def-B signature hard-fails the ratchet as ``envelope_in_core``.
        """
        import ast
        import inspect

        signature = inspect.signature(HandlerTenantRegistryProjectionRunner.handle)
        assert list(signature.parameters) == ["self", "request"]

        module = __import__(
            "omnimarket.nodes.node_projection_tenant_registry.handlers."
            "handler_tenant_registry_projection",
            fromlist=["x"],
        )
        # Parse the imports rather than grepping the text: the module's own
        # docstring names these types in order to say it does NOT use them,
        # and a substring check cannot tell the difference between a
        # prohibition and a violation.
        tree = ast.parse(inspect.getsource(module))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                imported.update(alias.name for alias in node.names)

        forbidden = {"ModelEventEnvelope", "ModelHandlerOutput", "PluginComputeBase"}
        assert not (imported & forbidden), (
            f"handler imports the non-canonical pre-def-B surface: "
            f"{sorted(imported & forbidden)}"
        )
        assert not any(
            base.__name__ in forbidden
            for base in HandlerTenantRegistryProjectionRunner.__mro__
        )

    def test_the_node_declares_exactly_one_table_and_no_serving_exposure(self) -> None:
        """The mirror is read at MIGRATION time, not by a dashboard caller.

        An exposure would put the whole slug<->uuid correspondence behind a
        serving path that then needs tenant scoping to be safe -- which is
        precisely the scoping this relation must not have.
        """
        import yaml

        from omnimarket.nodes import node_projection_tenant_registry

        contract_path = (
            __import__("pathlib").Path(node_projection_tenant_registry.__file__).parent
            / "contract.yaml"
        )
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        tables = contract["db_io"]["db_tables"]
        assert [t["name"] for t in tables] == ["tenant_registry_mirror"]
        assert "projection_api" not in contract


class TestControlPlaneTopicExemption:
    """The contract-sweep carve-out for `onex.tenant.events` (OMN-16930).

    A structural naming rule with an escape hatch is only as good as the hatch's
    shape. This pins that the exemption is an exact-match set and cannot be
    widened into a pattern by accident.
    """

    def test_the_exemption_is_an_exact_match_set_not_a_pattern(self) -> None:
        from omnimarket.nodes.node_contract_sweep.handlers.handler_contract_sweep import (
            _CONTROL_PLANE_TOPICS,
        )

        assert frozenset({"onex.tenant.events"}) == _CONTROL_PLANE_TOPICS

    def test_a_similar_but_unlisted_topic_is_still_a_violation(self) -> None:
        """The falsifier. Without this, "exact-match set" is an unverified claim.

        A near-miss name must still be rejected -- otherwise the carve-out is
        a hole rather than a door.
        """
        from omnimarket.nodes.node_contract_sweep.handlers.handler_contract_sweep import (
            _CONTROL_PLANE_TOPICS,
            _SNAPSHOT_TOPIC_RE,
            _TOPIC_RE,
        )

        for impostor in (
            "onex.tenant.event",
            "onex.tenants.events",
            "onex.billing.events",
            "onex.tenant.events.v1",
        ):
            excused = bool(
                _TOPIC_RE.match(impostor)
                or _SNAPSHOT_TOPIC_RE.match(impostor)
                or impostor in _CONTROL_PLANE_TOPICS
            )
            assert not excused, (
                f"{impostor} must not be excused by the control-plane carve-out"
            )
