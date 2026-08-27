# SPDX-License-Identifier: MIT
"""Projection contract ``access`` must cover the handler's operations (OMN-16690).

THE DEFECT. A ``db_io`` contract declares a per-table ``access`` capability.
The runtime enforces it fail-closed at
``omnibase_infra.runtime.auto_wiring.handler_wiring.ProjectionTableOperation``:
``_assert_read_declared`` admits only ``{"read", "read_write"}``. Thirteen
omnimarket projection nodes declared ``access: write`` on a table their handler
**reads** — an idempotency lookup before the upsert. Every event on those paths
raised ``PermissionError`` at the read, the projection arm's generic
``except Exception`` routed the envelope to the platform quarantine sink, the
offset committed, and **zero rows were ever written** while the gateway kept
answering 202.

WHY THE EXISTING TESTS ALL PASSED (the real-dispatch-path lesson). Every
projection test in this repo injects a hand-written fake adapter that
implements ``upsert``/``query`` as plain dict operations — with **no access
enforcement at all**. ``test_omn16090_real_dispatch_path.py`` drives the real
``_make_projection_dispatch_callback`` and still passed against the broken
contract, because its ``_FakeAdapter`` never consulted ``table.access``. The
enforcement that fires in production was simply absent from the harness. So
this file does the one thing that reproduces the live failure: it builds the
**real** ``ProjectionTableOperation`` over the **real** contract-declared
``ModelDbTableDeclaration``, and fakes only the psycopg2 SQL execution
underneath it. Capability enforcement is production code here, not a stub.

RED-before / GREEN-after (captured 2026-08-27 on `dev` @ fecf14bb, before the
contract fix in this PR)::

    FAILED test_real_dispatch_path_does_not_quarantine_the_event
    E   PermissionError: public.hook_events declares access='write'; read refused

    FAILED test_no_projection_contract_declares_less_than_its_handler_uses
    E   AssertionError: 14 projection table declaration(s) narrower than handler usage

Matching the live pod log this ticket was opened from
(``omninode-runtime-effects-849bcb486-6sftf``, ns ``onex-dev``).
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from omnibase_core.enums.enum_database_schema_domain import EnumDatabaseSchemaDomain
from omnibase_core.models.contracts.subcontracts.model_db_table_declaration import (
    ModelDbTableDeclaration,
)
from omnibase_infra.runtime.auto_wiring import handler_wiring
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    ProjectionDatabaseBindingTarget,
    ProjectionDatabaseTarget,
    ProjectionTableOperation,
    ProjectionTableTarget,
    _make_projection_dispatch_callback,
)

from omnimarket.nodes.node_hook_event_capture.handlers.handler_hook_event_capture import (
    TABLE,
    HandlerHookEventCapture,
)

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
NODES_DIR = REPO_ROOT / "src" / "omnimarket" / "nodes"
NODE_DIR = NODES_DIR / "node_hook_event_capture"

COMMAND_TOPIC = "onex.cmd.omnimarket.hook-event-capture-requested.v1"
_DSN_ENV = "OMNIMARKET_TEST_HOOK_EVENTS_DSN"
_PATCH_BUILD_ADAPTER = (
    "omnibase_infra.runtime.auto_wiring.handler_wiring._build_projection_db_adapter"
)

# The gate module is the single detection implementation, shared by the CI gate,
# the pre-commit hook and these tests, so the three can never disagree.
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))
from check_projection_contract_access import (  # noqa: E402
    Violation,
    scan,
)


# --------------------------------------------------------------------------
# The access-enforcing adapter: REAL enforcement, faked SQL.
# --------------------------------------------------------------------------
class _SqlRecorder:
    """Stands in for psycopg2 execution ONLY — never for capability checks."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def _execute_upsert(
        self,
        target: ProjectionTableTarget,
        conflict_key: str,
        row: dict[str, object],
        *,
        tenant_context: object | None,
    ) -> bool:
        self.rows.append(dict(row))
        return True

    def _execute_query(
        self,
        target: ProjectionTableTarget,
        filters: dict[str, object] | None,
        *,
        tenant_context: object | None,
    ) -> list[dict[str, object]]:
        applied = filters or {}
        return [
            dict(row)
            for row in self.rows
            if all(row.get(key) == value for key, value in applied.items())
        ]


class _AccessEnforcingAdapter:
    """``DatabaseAdapter`` protocol backed by REAL ``ProjectionTableOperation``.

    This is the whole point of the file. The production adapter routes each
    ``query``/``upsert`` through a ``ProjectionTableOperation`` built from the
    contract's own ``ModelDbTableDeclaration``, and that operation is what
    raises ``PermissionError`` on an undeclared capability. Reusing the real
    class here means the declaration under test is enforced by the same code
    that enforces it in the pod.
    """

    def __init__(self, table_targets: tuple[ProjectionTableTarget, ...]) -> None:
        self.recorder = _SqlRecorder()
        self._ops = {
            target.table.name: ProjectionTableOperation(self.recorder, target)  # type: ignore[arg-type]
            for target in table_targets
        }

    def upsert(self, table: str, conflict_key: str, row: dict[str, Any]) -> bool:
        return self._ops[table].upsert(conflict_key, row)

    def query(
        self, table: str, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return self._ops[table].query(filters)


def _real_table_target() -> ProjectionDatabaseTarget:
    """Target built from THIS node's REAL contract-declared table.

    ``access`` is read straight off the shipped ``contract.yaml`` — the field
    under test is never overridden, so a regression in the declaration fails
    this test rather than being papered over by a fixture.
    """
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    declared = contract["db_io"]["db_tables"][0]
    table = ModelDbTableDeclaration(**declared)
    binding = ProjectionDatabaseBindingTarget(
        binding_ref="tenant_projection",
        database_ref=table.database_ref,
        physical_database="application",
        principal="tenant_projection_writer",
        dsn_env=_DSN_ENV,
    )
    table_target = ProjectionTableTarget(
        table=table,
        database_ref=table.database_ref,
        physical_database="application",
        physical_schema=table.schema,
        domain=EnumDatabaseSchemaDomain.TENANT,
        read_binding=binding,
        write_binding=binding,
    )
    return ProjectionDatabaseTarget(
        tables=(table,),
        table_targets=(table_target,),
        physical_database="application",
    )


def _gateway_wire_payload() -> dict[str, Any]:
    """The OMN-16667 canary shape: what the gateway actually puts on the bus."""
    return {
        "source": "local_macos_claude_hooks",
        "batch_sha": "b" * 64,
        "events": [
            {
                "event_type": "onex.evt.omniclaude.skill-started.v1",
                "event_sha": "a" * 64,
                "occurred_at": "2026-08-16T18:00:00Z",
                "payload_json": '{"skill_name": "node_dod_verify"}',
            }
        ],
        "correlation_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "emitted_at": "2026-08-16T18:00:00Z",
        "tenant_id": "omninode",
        "tenant_principal_id": "t-" + "a" * 32,
    }


# --------------------------------------------------------------------------
# AC1 / AC5-shaped: the real dispatch path must not quarantine.
# --------------------------------------------------------------------------
def test_real_dispatch_path_does_not_quarantine_the_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact production seam: real callback, real handler, real access guard.

    Drives ``_make_projection_dispatch_callback`` — the function
    ``wire_from_manifest`` builds for a ``db_io`` contract — against the real
    ``HandlerHookEventCapture`` with an adapter whose capability checks are the
    production ``ProjectionTableOperation``. Asserts BOTH halves of the live
    symptom are gone: no event routed to the DLQ/quarantine sink, and a row
    actually lands. A green 202 with zero rows is precisely the failure this
    ticket exists to kill, so "no exception" alone would not be evidence.
    """
    monkeypatch.setenv(_DSN_ENV, "postgresql://unused-because-sql-is-faked")
    target = _real_table_target()
    adapter = _AccessEnforcingAdapter(target.table_targets)

    quarantined: list[str] = []

    async def _spy_route(
        event_bus: object | None,
        dlq_topics: list[str],
        envelope: object,
        handler_name: str,
        failure_reason: str,
    ) -> bool:
        quarantined.append(f"{handler_name}: {failure_reason}")
        return True

    callback = _make_projection_dispatch_callback(
        HandlerHookEventCapture(),
        target,
        (COMMAND_TOPIC,),
    )

    envelope = MagicMock()
    envelope.topic = COMMAND_TOPIC
    envelope.event_type = COMMAND_TOPIC
    envelope.payload = _gateway_wire_payload()

    def _inject_adapter(*_args: object, **_kwargs: object) -> _AccessEnforcingAdapter:
        return adapter

    monkeypatch.setattr(_PATCH_BUILD_ADAPTER, _inject_adapter)
    monkeypatch.setattr(
        handler_wiring, "_route_projection_error_to_dlq", _spy_route, raising=True
    )
    asyncio.run(callback(envelope))

    assert not quarantined, (
        "the event was routed to the quarantine/DLQ sink — the projection "
        f"handler was refused a declared capability: {quarantined}"
    )
    assert len(adapter.recorder.rows) == 1, (
        "no row landed through the REAL dispatch path with REAL access "
        "enforcement — a 202 with zero rows is the exact silent black hole "
        "OMN-16690 reports"
    )
    assert adapter.recorder.rows[0]["event_sha"] == "a" * 64


def test_redelivery_is_idempotent_through_the_enforcing_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read the contract now declares is the one that makes replay safe.

    Guards the fix from being "corrected" by deleting the handler's read: a
    second delivery of the same batch must not add a row. If someone removes
    the idempotency read to satisfy a write-only declaration, this fails.
    """
    monkeypatch.setenv(_DSN_ENV, "postgresql://unused-because-sql-is-faked")
    target = _real_table_target()
    adapter = _AccessEnforcingAdapter(target.table_targets)

    callback = _make_projection_dispatch_callback(
        HandlerHookEventCapture(), target, (COMMAND_TOPIC,)
    )

    def _envelope() -> MagicMock:
        env = MagicMock()
        env.topic = COMMAND_TOPIC
        env.event_type = COMMAND_TOPIC
        env.payload = _gateway_wire_payload()
        return env

    def _inject_adapter(*_args: object, **_kwargs: object) -> _AccessEnforcingAdapter:
        return adapter

    monkeypatch.setattr(_PATCH_BUILD_ADAPTER, _inject_adapter)
    asyncio.run(callback(_envelope()))
    asyncio.run(callback(_envelope()))

    assert len(adapter.recorder.rows) == 1, (
        "redelivery wrote a second row: the contract-declared read is what "
        "provides DO-NOTHING semantics the adapter's upsert lacks"
    )


def test_write_only_declaration_is_still_refused_at_the_read_seam() -> None:
    """The guard stays fail-closed — the fix is the contract, not the guard.

    Pins the runtime behaviour this ticket must NOT change. If a future edit
    relaxes ``_assert_read_declared`` to make the symptom disappear, this test
    fails and says why that is the wrong fix.
    """
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    declared = dict(contract["db_io"]["db_tables"][0])
    declared["access"] = "write"  # deliberately narrow, the pre-fix declaration
    table = ModelDbTableDeclaration(**declared)
    binding = ProjectionDatabaseBindingTarget(
        binding_ref="tenant_projection",
        database_ref=table.database_ref,
        physical_database="application",
        principal="tenant_projection_writer",
        dsn_env=_DSN_ENV,
    )
    target = ProjectionTableTarget(
        table=table,
        database_ref=table.database_ref,
        physical_database="application",
        physical_schema=table.schema,
        domain=EnumDatabaseSchemaDomain.TENANT,
        read_binding=binding,
        write_binding=binding,
    )
    adapter = _AccessEnforcingAdapter((target,))

    with pytest.raises(PermissionError, match="read refused"):
        adapter.query(TABLE, {"event_sha": "a" * 64})


# --------------------------------------------------------------------------
# AC2 / AC3: the class-level sweep and the gate that keeps it closed.
# --------------------------------------------------------------------------
def test_no_projection_contract_declares_less_than_its_handler_uses() -> None:
    """AC2: the whole repo, not just the two nodes the ticket named.

    The ticket was filed on two observed instances; the sweep behind this
    assertion found 14 mismatched tables across 13 nodes. Driven by the same
    ``scan()`` the CI gate runs, so the gate cannot pass while this fails.
    """
    violations = scan(REPO_ROOT)
    assert not violations, (
        f"{len(violations)} projection table declaration(s) narrower than "
        "handler usage — every event on these paths is quarantined:\n"
        + "\n".join(f"  - {v.render()}" for v in violations)
    )


def test_every_projection_contract_declares_a_valid_access_mode() -> None:
    """A typo like ``acccess:`` or ``write_read`` must not read as 'no read'."""
    valid = {"read", "write", "read_write"}
    bad: list[str] = []
    for contract_path in sorted(NODES_DIR.glob("*/contract.yaml")):
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            continue
        for table in (raw.get("db_io") or {}).get("db_tables") or []:
            access = table.get("access")
            if access not in valid:
                bad.append(
                    f"{contract_path.parent.name}/{table.get('name')}: "
                    f"access={access!r} not in {sorted(valid)}"
                )
    assert not bad, "invalid access declaration(s):\n" + "\n".join(bad)


def test_gate_detects_a_synthetic_violation(tmp_path: pathlib.Path) -> None:
    """The gate is RED-derivable: it fails on a planted defect.

    A gate that cannot fail is not a gate. Plants the exact shape of the live
    defect — ``access: write`` plus a handler that queries the same table — in
    a throwaway tree and asserts ``scan()`` reports it.
    """
    node = tmp_path / "src" / "omnimarket" / "nodes" / "node_projection_synthetic"
    (node / "handlers").mkdir(parents=True)
    (node / "contract.yaml").write_text(
        "db_io:\n"
        "  db_tables:\n"
        "    - name: synthetic_rows\n"
        "      database_ref: application\n"
        "      schema: public\n"
        "      access: write\n",
        encoding="utf-8",
    )
    (node / "handlers" / "handler_synthetic.py").write_text(
        'TABLE = "synthetic_rows"\n'
        "def handle(db):\n"
        "    if db.query(TABLE, {'id': 1}):\n"
        "        return 0\n"
        "    return db.upsert(TABLE, 'id', {'id': 1})\n",
        encoding="utf-8",
    )

    violations = scan(tmp_path)

    assert len(violations) == 1, f"gate missed the planted defect: {violations}"
    found: Violation = violations[0]
    assert found.table == "synthetic_rows"
    assert found.declared == "write"
    assert found.operation == "read"
    assert "read_write" in found.render()


def test_gate_ignores_reads_that_only_exist_in_tests(tmp_path: pathlib.Path) -> None:
    """A test's fake-adapter ``query`` says nothing about runtime capability.

    Several nodes (OMN-15707) deliberately removed the handler read and now
    assert in tests that no ``.query()`` happens — those tests call ``query``
    on their own fakes. Counting them would produce false violations and press
    contracts to over-declare ``read_write`` they do not need.
    """
    node = tmp_path / "src" / "omnimarket" / "nodes" / "node_projection_writeonly"
    (node / "handlers").mkdir(parents=True)
    (node / "tests").mkdir(parents=True)
    (node / "contract.yaml").write_text(
        "db_io:\n"
        "  db_tables:\n"
        "    - name: writeonly_rows\n"
        "      database_ref: application\n"
        "      schema: public\n"
        "      access: write\n",
        encoding="utf-8",
    )
    (node / "handlers" / "handler_writeonly.py").write_text(
        'TABLE = "writeonly_rows"\n'
        "def handle(db):\n"
        "    return db.upsert(TABLE, 'id', {'id': 1})\n",
        encoding="utf-8",
    )
    (node / "tests" / "test_writeonly.py").write_text(
        'TABLE = "writeonly_rows"\n'
        "def test_no_read(db):\n"
        "    assert db.query(TABLE) == []\n",
        encoding="utf-8",
    )

    assert scan(tmp_path) == [], "a test-only query was miscounted as a handler read"


def test_gate_resolves_table_constants_per_file(tmp_path: pathlib.Path) -> None:
    """Per-file constant resolution — a node-wide map hides real violations.

    ``node_projection_delegation`` ships several handler modules that each
    define their own ``TABLE``. Resolving constants across the whole node let a
    later module's ``TABLE`` overwrite an earlier one, which is how
    ``delegation_events`` (three read sites) was missed on the first sweep of
    this very ticket. This pins the per-file behaviour.
    """
    node = tmp_path / "src" / "omnimarket" / "nodes" / "node_projection_multi"
    (node / "handlers").mkdir(parents=True)
    (node / "contract.yaml").write_text(
        "db_io:\n"
        "  db_tables:\n"
        "    - name: alpha_rows\n"
        "      database_ref: application\n"
        "      schema: public\n"
        "      access: write\n"
        "    - name: beta_rows\n"
        "      database_ref: application\n"
        "      schema: public\n"
        "      access: read_write\n",
        encoding="utf-8",
    )
    (node / "handlers" / "handler_alpha.py").write_text(
        'TABLE = "alpha_rows"\ndef handle(db):\n    return db.query(TABLE)\n',
        encoding="utf-8",
    )
    (node / "handlers" / "handler_beta.py").write_text(
        'TABLE = "beta_rows"\ndef handle(db):\n    return db.query(TABLE)\n',
        encoding="utf-8",
    )

    violations = scan(tmp_path)

    assert [v.table for v in violations] == ["alpha_rows"], (
        "per-file constant resolution failed: a node-wide TABLE map masks the "
        f"alpha_rows read, got {violations}"
    )
