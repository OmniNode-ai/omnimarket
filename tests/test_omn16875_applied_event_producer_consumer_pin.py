# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16875 AC4 — pin the applied event's producer and consumer to ONE shape.

``onex.evt.omnimarket.projection-consumer-flow-applied.v1`` has exactly two
ends, and until this file they were pinned to two separate *copies* of the
shape rather than to each other:

* **Producer end** — ``tests/test_omn16874_consumer_flow_writer_dispatch.py::
  test_applied_payload_carries_the_window_facts_not_a_bare_ack`` drives the real
  ``ConsumerFlowProjectionWriter`` but only checks that eight field *names* are
  present as strings. It never asks whether the consumer can read them.
* **Consumer end** — ``tests/test_omn16778_stall_alert_self_assembly.py::
  test_the_applied_event_payload_validates_as_the_declared_input_model``
  resolves ``input_model`` dynamically from ``contract.yaml`` — real teeth
  against contract/model drift — but validates a hand-authored literal
  (``_applied_event_payload``), not producer output.
* ``tests/test_golden_chain_consumer_flow_stall_alert.py`` starts at the
  projection's terminal event by contract *declaration*, never by running the
  writer.

So a producer-side change could rename, drop, or re-type a field and no gate
would fire: the producer test asserts against its own literal expectations and
the consumer test asserts against its own literal fixture. That is precisely
AC4's falsification clause — "the two models being able to drift apart again
without a gate firing" — and it is the failure class that already cost this
chain two live outages (``{"projected": True}`` discarding every window fact,
then ``8 validation errors``, 94 of them in two minutes, when the shapes
disagreed).

**The seam spans two repos, and this file is explicit about where it stops.**
The applied event's payload is assembled by
``handler_wiring._build_projection_terminal_payload`` in ``omnibase_infra``,
which takes the projection handler's return value and *adds* keys to it
(``projected`` is exactly such an addition, kept for existing Pattern-B
consumers). ``omnimarket`` cannot import that function, so rather than
hand-copying the composed payload — the very move that created this gap — the
runtime's contribution is declared once here as
``_RUNTIME_EMITTER_ADDED_KEYS`` and then *proven to be exactly the difference*
between what the real writer returns and what the real consumer model requires.
If the writer stops supplying a required field, or the consumer model starts
requiring one nobody supplies, that difference changes and these tests fail.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_projection_consumer_flow.handlers.handler_consumer_flow_runner import (
    ConsumerFlowProjectionWriter,
)

_ALERT_NODE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_consumer_flow_stall_alert_effect"
)
_ALERT_CONTRACT_PATH = _ALERT_NODE_ROOT / "contract.yaml"

# The keys the runtime's generic projection emitter adds on top of the
# projection handler's own return value. Declared, not copied: every test below
# proves this set is exactly the producer/consumer difference rather than
# assuming it.
_RUNTIME_EMITTER_ADDED_KEYS = frozenset({"projected"})

_T0 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
_IN_TOPIC = "onex.evt.platform.node-heartbeat.v1"  # onex-topic-allow: real topic from the OMN-16755 incident
_STALLED_GROUP = "local.omnimarket.node_registration_orchestrator.consume"
_NODE_ID = uuid4()


def _declared_input_model() -> type[Any]:
    """Resolve the alert node's declared ``input_model`` from its contract.

    Resolved dynamically rather than imported by name so that a contract which
    declares one model while the node ships another fails here too.
    """
    raw = yaml.safe_load(_ALERT_CONTRACT_PATH.read_text(encoding="utf-8"))
    module_path, class_name = str(raw["input_model"]).rsplit(".", 1)
    resolved: type[Any] = getattr(importlib.import_module(module_path), class_name)
    return resolved


def _required_field_names(model: type[Any]) -> frozenset[str]:
    """The field names a pydantic model refuses to be built without."""
    return frozenset(
        name for name, field in model.model_fields.items() if field.is_required()
    )


class _LoopBoundPool:
    """Reduced asyncpg: usable only from the loop that created it."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.closed = False

    def check(self) -> None:
        if self.closed:
            raise RuntimeError("pool is closed")
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Event loop is closed")


class _RecordingAdapter:
    """Stands in for ``AsyncpgAdapter`` and enforces its loop affinity.

    The transport is injected; the row this returns is what Postgres hands back
    from the writer's ``RETURNING`` clause, so everything downstream of it —
    ``_wire_row``'s JSON reduction and the returned payload shape — is the real
    production code path, not a stand-in.
    """

    def __init__(self) -> None:
        self._pool: _LoopBoundPool | None = None
        self.rows: list[tuple[str, tuple[Any, ...]]] = []

    async def connect(self) -> None:
        self._pool = _LoopBoundPool()

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.closed = True
            self._pool = None

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        assert self._pool is not None, "call connect() first"
        self._pool.check()
        self.rows.append((query, params))
        if "SELECT COUNT(*)" in query:
            return [{"window_count": 0, "produced": 0}]
        if "MAX(ingest_sequence)" in query:
            return [{"last_sequence": None}]
        if "RETURNING" in query:
            return [_database_row()]
        return []


def _database_row() -> dict[str, Any]:
    """One accepted ``consumer_flow_windows`` row, as Postgres returns it.

    Native types on purpose — ``datetime`` for the window bounds, not strings —
    so the writer's own ``_wire_row`` reduction is exercised rather than
    bypassed by a pre-flattened fixture.
    """
    return {
        "consumer_group": _STALLED_GROUP,
        "topic": _IN_TOPIC,
        "window_start": _T0,
        "window_end": _T0 + timedelta(seconds=60),
        "node_id": str(_NODE_ID),
        "ingest_sequence": 1,
        "messages_in": 229150,
        "messages_out": 0,
        "messages_dlq": 0,
        "handler_errors": 0,
        "upstream_produced": None,
        "upstream_evidence": "UNOBSERVED",
        "flow_state": "STALLED",
        "evaluated_at": _T0 + timedelta(seconds=60),
    }


def _heartbeat(sequence: int) -> dict[str, Any]:
    start = _T0 + timedelta(seconds=60 * sequence)
    return {
        "flow_window": {
            "node_id": str(_NODE_ID),
            "window_start": start.isoformat(),
            "window_end": (start + timedelta(seconds=60)).isoformat(),
            "window_sequence": sequence,
            "consumer_deltas": [
                {
                    "consumer_group": _STALLED_GROUP,
                    "topic": _IN_TOPIC,
                    "node_id": str(_NODE_ID),
                    "window_start": start.isoformat(),
                    "window_end": (start + timedelta(seconds=60)).isoformat(),
                    "window_sequence": sequence,
                    "messages_in": 229150,
                    "messages_out": 0,
                    "messages_dlq": 0,
                    "handler_errors": 0,
                }
            ],
            "produce_deltas": [],
        },
        "_topic": _IN_TOPIC,
    }


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> ConsumerFlowProjectionWriter:
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql://fixture/db")
    instance = ConsumerFlowProjectionWriter()
    instance._db = _RecordingAdapter()  # type: ignore[assignment]
    # The snapshot publish is a separate seam with its own transport; this file
    # is about the payload the applied event carries.
    instance._snapshot_exposure = None
    return instance


@pytest.fixture
def producer_payload(writer: ConsumerFlowProjectionWriter) -> dict[str, Any]:
    """What the real writer returns — the applied event's payload, verbatim."""
    return writer.handle(_heartbeat(1))


@pytest.mark.unit
def test_the_real_writer_output_validates_as_the_declared_consumer_input_model(
    producer_payload: dict[str, Any],
) -> None:
    """AC4: the shape the producer emits is the shape the consumer declares.

    No hand-authored fixture stands between the two ends. The payload here came
    out of ``ConsumerFlowProjectionWriter.handle()``; the model came out of the
    alert node's ``contract.yaml``. If either moves without the other, this
    fails.
    """
    declared = _declared_input_model()

    trigger = declared.model_validate(
        {**producer_payload, **dict.fromkeys(_RUNTIME_EMITTER_ADDED_KEYS, True)}
    )

    assert trigger.rows_upserted == 1
    assert trigger.alerting_keys() == ((_STALLED_GROUP, _IN_TOPIC),)


@pytest.mark.unit
def test_the_runtime_emitter_supplies_exactly_the_keys_the_writer_does_not(
    producer_payload: dict[str, Any],
) -> None:
    """The cross-repo boundary is proven, not assumed.

    ``omnimarket`` cannot import ``handler_wiring``, so the runtime's
    contribution is declared once as ``_RUNTIME_EMITTER_ADDED_KEYS``. This
    asserts that declaration IS the difference between what the writer produces
    and what the consumer requires — so the gap cannot silently widen from
    either side:

    * writer drops a required field  -> the difference grows -> fail
    * consumer requires a new field nobody produces -> difference grows -> fail
    * writer starts supplying ``projected`` itself -> difference shrinks -> fail
    """
    required = _required_field_names(_declared_input_model())
    supplied_by_writer = frozenset(producer_payload)

    assert required - supplied_by_writer == _RUNTIME_EMITTER_ADDED_KEYS, (
        "the producer/consumer difference moved: the alert node requires "
        f"{sorted(required)}, the writer supplies {sorted(supplied_by_writer)}. "
        "Only the runtime emitter's documented additions "
        f"({sorted(_RUNTIME_EMITTER_ADDED_KEYS)}) may be missing here."
    )


@pytest.mark.unit
def test_every_field_the_consumer_reads_off_a_row_survives_the_writers_reduction(
    producer_payload: dict[str, Any],
) -> None:
    """``_wire_row`` must not drop or re-type what the alert node reads.

    The row model is resolved off the declared trigger's own annotation rather
    than imported by name, so renaming the nested model on the consumer side
    cannot quietly orphan this assertion.
    """
    declared = _declared_input_model()
    row_model = declared.model_fields["flow_rows"].annotation.__args__[0]

    rows = producer_payload["flow_rows"]
    assert len(rows) == 1, "the writer accepted one row; the payload must carry it"

    row = row_model.model_validate(rows[0])

    assert row.consumer_group == _STALLED_GROUP
    assert row.topic == _IN_TOPIC
    # The database hands back a native datetime; the consumer must still be able
    # to read it after ``_wire_row`` reduces it for the wire.
    assert row.window_start == _T0
    assert row.flow_state.value == "STALLED"
    assert str(row.node_id) == str(_NODE_ID)


@pytest.mark.unit
def test_a_bare_ack_is_refused_by_the_declared_consumer_input_model() -> None:
    """The regression this whole chain exists to prevent, stated as a gate.

    ``{"projected": True}`` is what the runtime published before OMN-16875, and
    what the alert node was handed on every trigger while it evaluated nothing.
    If a future change reverts the producer to a bare ack, the test above stops
    passing and this one states why.
    """
    declared = _declared_input_model()

    with pytest.raises(Exception, match=r"flow_rows|rows_upserted"):
        declared.model_validate({"projected": True})


@pytest.mark.unit
def test_an_empty_window_still_satisfies_the_consumer_contract(
    writer: ConsumerFlowProjectionWriter,
) -> None:
    """A priming tick writes nothing, and that must still be a readable trigger.

    ``rows_upserted: 0`` with an empty batch is a real answer, not an error —
    the alert node evaluates nothing and stays silent. It must not be a
    validation failure, or every boot would DLQ its first message.
    """
    declared = _declared_input_model()

    payload = writer.handle({"_topic": _IN_TOPIC})

    trigger = declared.model_validate(
        {**payload, **dict.fromkeys(_RUNTIME_EMITTER_ADDED_KEYS, True)}
    )
    assert trigger.rows_upserted == 0
    assert trigger.alerting_keys() == ()
