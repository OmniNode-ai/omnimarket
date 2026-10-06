# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20651: an unattributed delegation TERMINAL writes no row.

Operator ruling 2026-10-06T17:27Z (OMN-19972 comment ece7ed2d): with
``ONEX_TENANT_ID`` empty and no tenant declared by the delegation, the writer
stamping its own configured tenant is a defect, not intended. Until this change
``terminal_write_tenant`` answered an unresolved tenant with the house stamp on
an insert-only arm (OMN-18565), and ``house_tenant_write_stamp`` returns the
writer's ``Settings.onex_tenant_id`` first -- which is how the omn19972 lane
wrote one row stamped ``local-ee67b16e7aab`` for a delegation that declared no
tenant (2026-10-04, readback MANIFEST d05e5bb9f4ac).

The invariant asserted here is shared by both writers: an unattributed terminal
WRITES NO ROW, under any writer setting and either enforcement setting, and a
declared, registry-resolvable tenant is still the row's tenant even when the
writer is configured with a different one. The refusal MECHANICS differ by
runtime, exactly as for the unattributable verdict (OMN-18565, see
``test_omn18565_writer_refusal_drift.py``): the sync kernel path returns zero
rows with a named ERROR log, because raising there withholds the offset and
wedges the partition (OMN-17379); the async runner routes the record to its DLQ.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import textwrap
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnimarket.config.settings import get_settings
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_projection_delegation.handlers import (
    handler_delegation,
    handler_projection_delegation,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
    ModelTaskDelegatedEvent,
)
from omnimarket.projection import tenant_isolation
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_isolation import TenantRequiredError
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
)

pytestmark = [pytest.mark.unit]

#: The writer tenant the omn19972 lane was configured with when it stamped the
#: unattributed row, and a second one, so "the writer's own tenant" is never a
#: single value the code could special-case.
_WRITER_TENANTS = ("", "local-ee67b16e7aab", "writer-b")
#: An absent tenant and two blank ones -- the shapes "declares no tenant" takes.
_NO_TENANT = (None, "", "   ")

#: Registry tenant A (declared and resolvable) for the positive controls.
_TENANT_A_SLUG = "omn20651-tenant-a"
_TENANT_A_UUID = "6a3f7c1e-2b4d-4e8f-9a10-1c2d3e4f5a6b"


WriterSettings = Callable[[str, bool], None]


@pytest.fixture
def writer_settings(monkeypatch: pytest.MonkeyPatch) -> WriterSettings:
    """Set the WRITER's configured tenant and enforcement for one test."""
    settings = get_settings()

    def _apply(onex_tenant_id: str, enforce: bool) -> None:
        monkeypatch.setattr(settings, "onex_tenant_id", onex_tenant_id, raising=True)
        monkeypatch.setattr(settings, "enforce_tenant_isolation", enforce, raising=True)

    return _apply


def _task_delegated(
    correlation_id: str, tenant_id: str | None
) -> ModelTaskDelegatedEvent:
    return ModelTaskDelegatedEvent(
        correlation_id=correlation_id,
        tenant_id=tenant_id,
        task_type="code-review",
        delegated_to="glm-5.2",
        model_name="glm-5.2",
        quality_gate_passed=True,
    )


def _delegate_skill_terminal(
    correlation_id: str, tenant_id: str | None
) -> ModelDelegateSkillTerminalProjection:
    payload: dict[str, Any] = {
        "status": "completed",
        "correlation_id": correlation_id,
        "task_type": "code-review",
        "quality_gate_passed": True,
        "quality_score": 0.9,
        "model_name": "glm-5.2",
        "attempts_count": 1,
    }
    if tenant_id is not None:
        payload["tenant_id"] = tenant_id
    return ModelDelegateSkillTerminalProjection.from_payload(payload)


def _db_with_tenant_a() -> InmemoryDatabaseAdapter:
    db = InmemoryDatabaseAdapter()
    db.upsert(
        TENANT_REGISTRY_MIRROR_TABLE,
        "tenant_slug",
        {"tenant_slug": _TENANT_A_SLUG, "tenant_uuid": _TENANT_A_UUID},
    )
    return db


def _drive_sync(
    which: str, db: InmemoryDatabaseAdapter, correlation_id: str, tenant: str | None
) -> int:
    """Run one sync terminal path; return rows_upserted, or -1 on a typed raise."""
    handler = HandlerProjectionDelegation()
    try:
        if which == "project":
            result = handler.project(_task_delegated(correlation_id, tenant), db)
        else:
            result = handler.project_delegate_skill_terminal(
                _delegate_skill_terminal(correlation_id, tenant), db
            )
    except TenantRequiredError:
        return -1
    return result.rows_upserted


_SYNC_PATHS = ("project", "project_delegate_skill_terminal")


class TestTheSyncTerminalPathsWriteNoRowWithoutATenant:
    """AC1/AC2, the deployed kernel path."""

    @pytest.mark.parametrize("which", _SYNC_PATHS)
    @pytest.mark.parametrize("tenant", _NO_TENANT)
    @pytest.mark.parametrize("writer_tenant", _WRITER_TENANTS)
    def test_no_row_and_a_named_refusal_with_enforcement_off(
        self,
        which: str,
        tenant: str | None,
        writer_tenant: str,
        writer_settings: WriterSettings,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        writer_settings(writer_tenant, False)
        db = InmemoryDatabaseAdapter()
        correlation_id = str(uuid4())

        with caplog.at_level(logging.ERROR):
            rows = _drive_sync(which, db, correlation_id, tenant)

        assert rows == 0, (
            f"{which} wrote a row (or raised) for a terminal declaring no tenant "
            f"with writer tenant {writer_tenant!r} -- OMN-20651 requires zero rows "
            "and no raise on the kernel seam"
        )
        assert db.query(TABLE, {"correlation_id": correlation_id}) == []
        refusals = [
            r.getMessage() for r in caplog.records if "OMN-20651" in r.getMessage()
        ]
        assert refusals, "the refusal must be logged and name OMN-20651"
        assert any("tenant" in m for m in refusals), (
            "the refusal must name the missing tenant"
        )
        assert any(correlation_id in m for m in refusals), (
            "the refusal must name the correlation"
        )

    @pytest.mark.parametrize("which", _SYNC_PATHS)
    @pytest.mark.parametrize("writer_tenant", _WRITER_TENANTS)
    def test_no_row_with_enforcement_on(
        self,
        which: str,
        writer_tenant: str,
        writer_settings: WriterSettings,
    ) -> None:
        writer_settings(writer_tenant, True)
        db = InmemoryDatabaseAdapter()
        correlation_id = str(uuid4())

        _drive_sync(which, db, correlation_id, None)

        assert db.query(TABLE, {"correlation_id": correlation_id}) == []


class TestADeclaredTenantStaysTheRowsTenant:
    """AC3 and the positive control for AC1/AC2: a writer that refused every
    terminal would satisfy the class above completely."""

    @pytest.mark.parametrize("which", _SYNC_PATHS)
    @pytest.mark.parametrize("writer_tenant", ["", "writer-b"])
    def test_sync_path_writes_exactly_one_row_under_tenant_a(
        self,
        which: str,
        writer_tenant: str,
        writer_settings: WriterSettings,
    ) -> None:
        writer_settings(writer_tenant, False)
        db = _db_with_tenant_a()
        correlation_id = str(uuid4())

        rows = _drive_sync(which, db, correlation_id, _TENANT_A_SLUG)

        stored = db.query(TABLE, {"correlation_id": correlation_id})
        assert rows == 1
        assert len(stored) == 1
        assert str(stored[0]["tenant_id"]) == _TENANT_A_UUID
        assert str(stored[0]["tenant_id"]) != writer_tenant


# ----------------------------------------------------------------- async runner


async def _publish(topic: str, value: bytes) -> None:
    return


def _async_runner(
    registry_uuid: str | None,
) -> tuple[DelegationProjectionRunner, AsyncMock, AsyncMock]:
    """The runner, its mock database and its DLQ router, as typed locals."""
    runner = DelegationProjectionRunner(publish_fn=_publish)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    db.fetchval = AsyncMock(return_value=registry_uuid)
    dlq = AsyncMock(return_value=True)
    runner._db = db
    # Instance dict, not attribute assignment: mypy refuses assigning to a
    # method, and the stub must shadow it on this instance only.
    vars(runner)["_route_malformed_to_dlq"] = dlq
    return runner, db, dlq


def _completed_payload(correlation_id: str, tenant: str | None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "correlation_id": correlation_id,
        "task_type": "code-review",
        "model_used": "glm-5.2",
        "content": "the model's real answer",
        "quality_passed": True,
        "quality_score": 0.95,
        "latency_ms": 1800,
        "prompt_tokens": 210,
        "completion_tokens": 480,
        "cumulative_attempt_cost": 0.0142,
        "cost_tier_name": "cheap_cloud",
    }
    if tenant is not None:
        data["tenant_id"] = tenant
    return data


def _skill_payload(correlation_id: str, tenant: str | None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "status": "completed",
        "correlation_id": correlation_id,
        "task_type": "code-review",
        "quality_gate_passed": True,
        "quality_score": 0.9,
        "model_name": "glm-5.2",
        "attempts_count": 1,
    }
    if tenant is not None:
        data["tenant_id"] = tenant
    return data


def _inserts(db: AsyncMock) -> list[Any]:
    return [
        c
        for c in db.execute.await_args_list
        if str(c.args[0]).strip().startswith(f"INSERT INTO {TABLE}")
    ]


_ASYNC_PATHS = ("delegation-completed", "delegate-skill-completed")


def _drive_async(
    which: str,
    runner: DelegationProjectionRunner,
    correlation_id: str,
    tenant: str | None,
) -> None:
    if which == "delegation-completed":
        topic, data = (
            runner._topic_delegation_completed,
            _completed_payload(correlation_id, tenant),
        )
    else:
        topic, data = (
            runner._topic_delegate_skill_completed,
            _skill_payload(correlation_id, tenant),
        )
    assert topic, f"contract must declare the {which} topic"
    meta = MessageMeta(partition=0, offset=20651, fallback_id=correlation_id)
    asyncio.run(runner.project_event(topic, data, meta))


class TestTheAsyncTerminalPathsWriteNoRowWithoutATenant:
    """AC1/AC2 on the standalone async runner."""

    @pytest.mark.parametrize("which", _ASYNC_PATHS)
    @pytest.mark.parametrize("tenant", _NO_TENANT)
    @pytest.mark.parametrize("writer_tenant", _WRITER_TENANTS)
    @pytest.mark.parametrize("enforce", [False, True])
    def test_no_insert_and_the_record_reaches_the_dlq(
        self,
        which: str,
        tenant: str | None,
        writer_tenant: str,
        enforce: bool,
        writer_settings: WriterSettings,
    ) -> None:
        writer_settings(writer_tenant, enforce)
        runner, db, dlq = _async_runner(registry_uuid=None)
        correlation_id = str(uuid4())

        raised = False
        try:
            _drive_async(which, runner, correlation_id, tenant)
        except TenantRequiredError:
            # With ENFORCE_TENANT_ISOLATION on, the pre-existing OMN-14898 guard
            # refuses first, by raising, before this ticket's check is reached.
            # That is still "no write"; how the runner handles the raise is not
            # changed here.
            raised = True

        assert _inserts(db) == [], (
            f"the async {which} path issued a delegation_events INSERT for a "
            f"terminal declaring no tenant (writer tenant {writer_tenant!r}, "
            f"enforce={enforce}) -- OMN-20651"
        )
        if enforce:
            assert raised or dlq.await_count == 1
        else:
            assert not raised
            assert dlq.await_count == 1

    @pytest.mark.parametrize("which", _ASYNC_PATHS)
    def test_a_declared_tenant_is_written_under_tenant_a(
        self,
        which: str,
        writer_settings: WriterSettings,
    ) -> None:
        writer_settings("writer-b", False)
        runner, db, dlq = _async_runner(registry_uuid=_TENANT_A_UUID)
        correlation_id = str(uuid4())

        _drive_async(which, runner, correlation_id, _TENANT_A_SLUG)

        inserts = _inserts(db)
        assert len(inserts) == 1
        assert _TENANT_A_UUID in [str(a) for a in inserts[0].args[1:]]
        assert "writer-b" not in [str(a) for a in inserts[0].args[1:]]
        assert dlq.await_count == 0


# ------------------------------------------------------------ structural guard


class TestNoTerminalPathCanReachTheHouseTenant:
    """The revert, and the next fallback shape not yet written, caught in code.

    Parsed rather than grepped so the prose explaining the defect stays legal
    (the same reasoning as ``test_omn18565_writer_refusal_drift``).
    """

    _HOUSE_REACHES = (
        "house_tenant_write_stamp",
        "HOUSE_TENANT_UUID",
        "820272f9",
        "onex_tenant_id",
    )

    @staticmethod
    def _sources() -> dict[str, str]:
        return {
            "terminal_write_tenant": inspect.getsource(
                tenant_isolation.terminal_write_tenant
            ),
            "sync.project": inspect.getsource(HandlerProjectionDelegation.project),
            "sync.project_delegate_skill_terminal": inspect.getsource(
                HandlerProjectionDelegation.project_delegate_skill_terminal
            ),
            "async._project_typed_event_async": inspect.getsource(
                handler_delegation.DelegationProjectionRunner._project_typed_event_async
            ),
            "async._upsert_delegate_skill_projection_row": inspect.getsource(
                handler_delegation.DelegationProjectionRunner._upsert_delegate_skill_projection_row
            ),
        }

    @staticmethod
    def _code_identifiers(source: str) -> set[str]:
        tree = ast.parse(textwrap.dedent(source))
        function = tree.body[0]
        assert isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
        if (
            function.body
            and isinstance(function.body[0], ast.Expr)
            and isinstance(function.body[0].value, ast.Constant)
            and isinstance(function.body[0].value.value, str)
        ):
            function.body = function.body[1:]
        names: set[str] = set()
        for node in ast.walk(function):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.add(node.value)
        return names

    @pytest.mark.parametrize(
        "where",
        [
            "terminal_write_tenant",
            "sync.project",
            "sync.project_delegate_skill_terminal",
            "async._project_typed_event_async",
            "async._upsert_delegate_skill_projection_row",
        ],
    )
    def test_names_no_house_or_writer_tenant_expression(self, where: str) -> None:
        names = self._code_identifiers(self._sources()[where])
        offenders = sorted(n for n in names for r in self._HOUSE_REACHES if r in n)
        assert not offenders, (
            f"{where} reaches the house or writer tenant in code via {offenders!r}; "
            "an unattributed delegation terminal must author no row (OMN-20651)"
        )

    def test_the_needle_is_live(self) -> None:
        """Positive control: a non-delegation writer still uses the house stamp
        under the unchanged 2026-08-02 ruling, so the needle above can match."""
        from omnimarket.nodes.node_projection_routing_decision.handlers import (
            handler_projection_routing_decision,
        )

        assert "house_tenant_write_stamp" in inspect.getsource(
            handler_projection_routing_decision
        )
        assert handler_projection_delegation is not None
