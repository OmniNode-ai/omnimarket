# SPDX-License-Identifier: MIT
"""node_hook_event_capture: the REAL dispatch path, not a hand-simulated one.

A test that merely calls ``handle({...})`` proves that the handler accepts a
dict — it does not prove the RUNTIME ever builds and calls it that way. The
defect this test exists to catch was exactly that gap: unit tests called
``handle(ModelHookEventCaptureRequest(...))`` directly and passed, while the
REAL dispatch path (``omnibase_infra.runtime.auto_wiring.handler_wiring
.wire_from_manifest``) always builds a plain dict for a ``db_io.db_tables``
contract — regardless of ``handler_routing``/``event_model`` — and calls
``handler_instance.handle(input_data)``. ``request.batch_sha`` on that dict
raised ``AttributeError``, silently swallowed by the arm's generic
``except Exception`` and routed to platform quarantine: offset committed,
consumer-group lag reads 0, zero rows land.

This test drives ``_make_projection_dispatch_callback`` — the exact function
``wire_from_manifest`` calls for a ``db_io`` contract at
``handler_wiring.py`` (``if db_tables: ... callback =
_make_projection_dispatch_callback(...)``) — against the REAL
``HandlerHookEventCapture``, with only the two boundary seams a unit test
should ever fake: the DSN-resolving ``_build_projection_db_adapter`` (would
otherwise open a real psycopg2 connection) and the ``ModelEventEnvelope``
transport wrapper (a ``MagicMock`` carrying ``.topic``/``.payload``, the exact
attributes ``_extract_projection_topic``/``_extract_projection_payload`` read
— matching ``omnibase_infra``'s own
``tests/integration/test_projection_handler_wiring_runtime_dispatch.py``
harness, the closest existing precedent for this exact seam-fake shape). The
``target: ProjectionDatabaseTarget`` is built from the REAL contract-declared
table (via ``ModelDbTableDeclaration``), not invented.

RED-before / GREEN-after evidence (captured 2026-08-18, `git stash` of the
OMN-16090 dispatch-shape fix against `dev`)::

    FAILED tests/test_omn16090_real_dispatch_path.py::test_real_dispatch_callback_persists_the_batch
    handler_hook_event_capture.py:105: in handle
        fallback_id=request.batch_sha,
    E   AttributeError: 'dict' object has no attribute 'batch_sha'

GREEN after the fix (this file, current tree): both tests below pass.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from omnibase_core.enums.enum_database_schema_domain import EnumDatabaseSchemaDomain
from omnibase_core.models.contracts.subcontracts.model_db_table_declaration import (
    ModelDbTableDeclaration,
)
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    ProjectionDatabaseBindingTarget,
    ProjectionDatabaseTarget,
    ProjectionDispatchSinks,
    ProjectionTableTarget,
    _make_projection_dispatch_callback,
)

from omnimarket.nodes.node_hook_event_capture.handlers.handler_hook_event_capture import (
    TABLE,
    HandlerHookEventCapture,
)

pytestmark = pytest.mark.unit

COMMAND_TOPIC = "onex.cmd.omnimarket.hook-event-capture-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.hook-events-captured.v1"
_DSN_ENV = "OMNIMARKET_TEST_HOOK_EVENTS_DSN"

_PATCH_BUILD_ADAPTER = (
    "omnibase_infra.runtime.auto_wiring.handler_wiring._build_projection_db_adapter"
)

NODE_DIR = (
    __import__("pathlib").Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_hook_event_capture"
)


class _FakeAdapter:
    """The injected ``_db``: single-row upsert/query, DO-NOTHING via query-first."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    def upsert(self, table: str, conflict_key: str, row: dict[str, Any]) -> bool:
        assert table == TABLE
        key = (str(row["tenant_id"]), str(row["event_sha"]))
        self.rows[key] = dict(row)
        return True

    def query(
        self, table: str, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        applied = filters or {}
        return [
            dict(row)
            for row in self.rows.values()
            if all(row.get(k) == v for k, v in applied.items())
        ]


def _real_table_target() -> ProjectionDatabaseTarget:
    """A ``ProjectionDatabaseTarget`` for THIS node's real contract-declared table.

    Built directly (not via full topology resolution) because the shipped
    topology instance's grants for ``tenant.hook_events`` are a separate,
    pre-existing pin-staleness gap (unrelated to this ticket's dispatch-shape
    defect) — see the OMN-15361-class table-grants drift already tracked
    elsewhere. ``_make_projection_dispatch_callback`` only reads
    ``target.bindings`` before dispatch (for the DSN-env presence check); the
    real DB adapter construction is faked below, so the binding's DSN value
    is never actually used.
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


def test_real_dispatch_callback_persists_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wire_from_manifest -> _make_projection_dispatch_callback -> handle().

    Drives the SAME callback the real runtime auto-wiring constructs and
    calls for this node's contract, against the REAL handler class. This is
    the test that was RED against the pre-fix ``handle(request:
    ModelHookEventCaptureRequest)`` signature — see the module docstring for
    the captured traceback.
    """
    monkeypatch.setenv(_DSN_ENV, "postgresql://unused-because-adapter-is-faked")
    fake_db = _FakeAdapter()

    callback = _make_projection_dispatch_callback(
        HandlerHookEventCapture(),
        _real_table_target(),
        (COMMAND_TOPIC,),
    )

    envelope = MagicMock()
    envelope.topic = COMMAND_TOPIC
    envelope.event_type = COMMAND_TOPIC
    envelope.payload = _gateway_wire_payload()

    with pytest.MonkeyPatch.context() as ctx:
        ctx.setattr(_PATCH_BUILD_ADAPTER, lambda *_args, **_kwargs: fake_db)
        result = asyncio.run(callback(envelope))

    assert result is None, "the projection callback never returns a dispatch result"
    assert len(fake_db.rows) == 1, (
        "the batch must land as exactly one row through the REAL dispatch "
        "chain — a dict-vs-model mismatch here silently drops every batch "
        "(offset committed, LAG=0, zero rows, quarantine only)"
    )
    (row,) = fake_db.rows.values()
    assert row["event_type"] == "onex.evt.omniclaude.skill-started.v1"
    assert row["batch_sha"] == "b" * 64


def test_real_dispatch_callback_emits_the_terminal_event_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The arm gates ITS OWN terminal-event emission on the handler's returned
    ``rows_upserted`` — proving ``handle()``'s return shape, not just its
    input shape, matches what the real dispatch arm requires."""
    monkeypatch.setenv(_DSN_ENV, "postgresql://unused-because-adapter-is-faked")
    fake_db = _FakeAdapter()
    fake_bus = MagicMock(spec=ProtocolEventBusPublisher)
    fake_bus.publish = AsyncMock()

    callback = _make_projection_dispatch_callback(
        HandlerHookEventCapture(),
        _real_table_target(),
        (COMMAND_TOPIC,),
        sinks=ProjectionDispatchSinks(
            event_bus=fake_bus, terminal_event=TERMINAL_TOPIC
        ),
    )

    envelope = MagicMock()
    envelope.topic = COMMAND_TOPIC
    envelope.event_type = COMMAND_TOPIC
    envelope.payload = _gateway_wire_payload()

    with pytest.MonkeyPatch.context() as ctx:
        ctx.setattr(_PATCH_BUILD_ADAPTER, lambda *_args, **_kwargs: fake_db)
        asyncio.run(callback(envelope))

    fake_bus.publish.assert_awaited_once()
    (topic, *_rest), _kwargs = fake_bus.publish.call_args
    assert topic == TERMINAL_TOPIC
