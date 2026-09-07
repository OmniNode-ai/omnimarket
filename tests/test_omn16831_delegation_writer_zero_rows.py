# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16831: the two writer defects that made ``delegation_events`` stay at 0.

READ-ONLY TRACE THIS FILE ENCODES (onex-dev, dev-system cluster
``i-06169517a92b45f86``, 2026-09-07T11:58Z-12:19Z). Two terminal delegations
(correlation ids ``5e2fce6b-...`` at 08:26Z and ``d5a69255-...`` at 09:54Z)
produced ZERO rows in ``delegation_events``. Kafka delivery was not implicated:
each delegation put exactly two events on the bus --
``quality-gate-result.v1`` and ``delegation-failed.v1`` -- the writer's consumer
group consumed both (``committed == end``, lag 0 on every subscribed
partition), and produced one DLQ record per source event. Every one of the four
was REJECTED INSIDE THE WRITER, by two independent defects, before any INSERT
was attempted. Grants and RLS were verified present and are not implicated
(``has_table_privilege(role_omnidash,'delegation_events','INSERT') = true``;
``live_events`` was writing at 302631 rows and rising over the same window).

DEFECT A -- the runner's own transport key fails an ``extra="forbid"`` model.
``ProjectionRunner._handle_message`` calls ``unwrap_envelope(msg.value)``, which
unconditionally re-attaches the whole raw wire message under ``_envelope``.
``ModelQualityGateResult`` and ``ModelDelegationJudgeVerdictEvent`` are both
``extra="forbid"``. So EVERY message on those two topics failed validation on
``_envelope`` -- ``Extra inputs are not permitted`` -- whatever the producer
sent. 53 of the 82 records on
``onex.dlq.omnimarket.projection-delegation-malformed.v1`` carry that failure,
first 2026-08-26T12:40:01.508Z, last 2026-09-07T11:28:53.769Z. The offset was
committed each time, so the loss was silent and lag read 0.

DEFECT B -- a canonical UUID matched against a TEXT slug column. The delegation
wire carries the gateway's verified tenant **UUID** in ``tenant_id``
(``ModelGatewayIdentity`` types ``tenant_id: UUID`` beside a separate
``tenant_slug: str``, and only the UUID is ever emitted). The writer passed that
value to a lookup keyed
``SELECT tenant_uuid FROM tenant_registry_mirror WHERE tenant_slug = $1``, which
never matched, so every terminal delegation raised
``TenantRegistryResolutionError`` -> POISON -> DLQ. 29 DLQ records, first
2026-09-07T06:20:07.803Z. The raised message blamed a registry that had not
"caught up"; on the live mirror a count by ``tenant_uuid`` returned 1 for the
same value a count by ``tenant_slug`` returned 0 for -- the registry had caught
up and the lookup key was wrong.

The two defects are independent: A is earliest in time, but B independently
kills the only event carrying the full terminal row, so fixing either alone
still yields zero rows. Every test below drives the REAL
``DelegationProjectionRunner.project_event`` over payloads produced by the REAL
``unwrap_envelope`` from REAL wire-shaped bytes -- the injection under test is
performed by the shipped code, not simulated by the fixture.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_registry_resolution import (
    TenantRegistryResolutionError,
)

DLQ_TOPIC = "onex.dlq.omnimarket.projection-delegation-malformed.v1"

# The tenant on the two onex-dev delegations. It is the SAME uuid the shipped
# legacy map holds for the ``beta-business-proof`` slug (see
# tests/test_omn15905_delegation_projection_writer_seam.py, which asserts that
# resolution), which is why it is safe to name here: it is already in this
# public tree, and it is not an external customer's identifier.
MIRRORED_TENANT_UUID = UUID("91c74442-1233-4c97-b191-911a10346fdf")
MIRRORED_TENANT_SLUG = "beta-business-proof"
# A provisioned-but-unmirrored identity. On the live mirror this UUID matched
# neither column -- the OMN-17446 historical-tenant gap. It must still refuse.
UNMIRRORED_TENANT_UUID = UUID("af432b87-4390-4c76-af97-ee9e158936e7")

CORRELATION_ID = "5e2fce6b-a8a1-448a-8aa5-1b88718a89d2"


def _wire_bytes(payload: dict[str, Any]) -> bytes:
    """The exact envelope shape the live broker records carry.

    Read off MSK for both onex-dev delegations: a top-level ``payload`` object
    beside ``envelope_id`` and ``envelope_version``. ``unwrap_envelope`` sees
    the ``payload`` key, returns a copy of it, and attaches this whole dict back
    under ``_envelope`` -- which is defect A, performed here by the shipped
    function rather than asserted about.
    """
    return json.dumps(
        {
            "payload": payload,
            "envelope_id": str(uuid4()),
            "envelope_version": {"major": 1, "minor": 0, "patch": 0, "build": None},
        }
    ).encode("utf-8")


def _runner_payload(payload: dict[str, Any]) -> dict[str, Any]:
    unwrapped = unwrap_envelope(_wire_bytes(payload))
    assert unwrapped is not None
    assert "_envelope" in unwrapped, (
        "the shipped unwrap_envelope must be the thing that injects _envelope; "
        "if this assertion fails the fixture no longer reproduces the defect"
    )
    return unwrapped


def _mock_db(*, mirror: dict[str, UUID] | None = None) -> AsyncMock:
    """An adapter double whose ``fetchval`` IS ``tenant_registry_mirror``.

    ``mirror`` maps the mirror's ``tenant_slug`` to its ``tenant_uuid``, exactly
    as the live relation does. The double answers the lookup by inspecting WHICH
    COLUMN the SQL predicates on, so a query keyed ``tenant_slug`` with a UUID
    argument returns no row -- which is precisely what the live database did.
    """
    rows = dict(mirror or {})
    by_uuid = {str(value): value for value in rows.values()}

    async def _fetchval(sql: str, param: object, *_a: Any, **_k: Any) -> object:
        if "WHERE tenant_uuid = $1" in sql:
            return by_uuid.get(str(param))
        if "WHERE tenant_slug = $1" in sql:
            return rows.get(str(param))
        raise AssertionError(f"unexpected registry lookup: {sql}")

    async def _execute(query: str, *_a: Any, **_k: Any) -> Any:
        return []

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.fetchval = AsyncMock(side_effect=_fetchval)
    return db


def _delegation_writes(db: AsyncMock) -> list[Any]:
    return [
        call
        for call in db.execute.await_args_list
        if str(call.args[0]).strip().startswith("INSERT INTO delegation_events")
    ]


def _param_by_column(call_args: tuple[object, ...]) -> dict[str, object]:
    sql = str(call_args[0])
    columns = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    return dict(zip(columns, call_args[1:], strict=True))


def _capture() -> tuple[list[tuple[str, bytes]], Any]:
    published: list[tuple[str, bytes]] = []

    async def capture_publish(topic: str, value: bytes) -> None:
        published.append((topic, value))

    return published, capture_publish


def _quality_gate_payload(*, correlation_id: str) -> dict[str, Any]:
    """The live ``quality-gate-result.v1`` payload, field-for-field.

    Matches ``ModelQualityGateResult`` and the pydantic repr the onex-dev
    writer logged at 08:26:15,771 for offset 216.
    """
    return {
        "correlation_id": correlation_id,
        "passed": False,
        "fail_category": "fail_deterministic",
        "quality_score": 0.40,
        "actual_score": 0.40,
        "failure_reasons": ["score_below_required_bar"],
        "score_source": "deterministic_acceptance",
    }


def _delegation_failed_payload(
    *, correlation_id: str, tenant_id: str
) -> dict[str, Any]:
    """The live ``delegation-failed.v1`` payload shape (offsets 63 and 65).

    ``tenant_id`` carries the gateway's canonical UUID -- that is the wire fact
    defect B turns on, confirmed by reading the broker record directly.
    """
    return {
        "correlation_id": correlation_id,
        "tenant_id": tenant_id,
        "task_type": "code-review",
        "model_used": "glm-5.2",
        "content": "",
        "quality_passed": False,
        "quality_score": 0.40,
        "latency_ms": 1800,
        "prompt_tokens": 210,
        "completion_tokens": 0,
        "cumulative_attempt_cost": 0.0142,
        "cost_tier_name": "cheap_cloud",
    }


def _judge_verdict_payload(*, correlation_id: str) -> dict[str, Any]:
    return {
        "correlation_id": correlation_id,
        "task_type": "research",
        "score_source": "reproducible_judge",
        "judge_model": "glm-5.2",
        "judge_model_version": "v1",
        "judge_provider": "zai",
        "rubric_id": "rubric-1",
        "rubric_hash": "sha256:" + "a" * 64,
        "prompt_hash": "sha256:" + "b" * 64,
        "input_hash": "sha256:" + "c" * 64,
        "temperature": 0.0,
        "judge_node_version": "1.0.0",
        "reasoning_hash": "sha256:" + "d" * 64,
        "verdict": "pass",
        "actual_score": 0.9,
        "failure_kind": None,
        "failure_message": None,
        "event_hash": "sha256:" + "e" * 64,
    }


@pytest.mark.unit
class TestDefectAEnvelopeKeyIsNotAProducerField:
    """The runner's own ``_envelope`` key must not fail an extra="forbid" model."""

    def test_quality_gate_result_writes_a_row_instead_of_quarantining(self) -> None:
        published, capture = _capture()
        runner = DelegationProjectionRunner(publish_fn=capture)
        db = _mock_db(mirror={MIRRORED_TENANT_SLUG: MIRRORED_TENANT_UUID})
        runner._db = db

        topic = runner._topic_quality_gate_result
        assert topic, "contract must declare a quality-gate-result topic"
        data = _runner_payload(_quality_gate_payload(correlation_id=CORRELATION_ID))
        meta = MessageMeta(partition=0, offset=216, fallback_id=CORRELATION_ID)

        ok = asyncio.run(runner.project_event(topic, data, meta))

        assert ok is True
        assert not [t for t, _ in published if t == DLQ_TOPIC], (
            "the runner's own transport key is not a malformed producer payload"
        )
        writes = _delegation_writes(db)
        assert len(writes) == 1, (
            "RED before the fix: zero writes, one DLQ record, offset committed"
        )
        by_column = _param_by_column(writes[0].args)
        assert by_column["correlation_id"] == CORRELATION_ID
        assert by_column["quality_gate_passed"] is False

    def test_a_real_unknown_producer_field_is_still_refused(self) -> None:
        """The strip removes exactly the transport keys, never widens the model."""
        published, capture = _capture()
        runner = DelegationProjectionRunner(publish_fn=capture)
        runner._db = _mock_db()

        payload = _quality_gate_payload(correlation_id=CORRELATION_ID)
        payload["not_a_field_any_producer_sends"] = "x"
        data = _runner_payload(payload)
        meta = MessageMeta(partition=0, offset=217, fallback_id=CORRELATION_ID)

        ok = asyncio.run(
            runner.project_event(runner._topic_quality_gate_result, data, meta)
        )

        assert ok is True
        dlq = [v for t, v in published if t == DLQ_TOPIC]
        assert len(dlq) == 1
        envelope = json.loads(dlq[0].decode("utf-8"))
        assert "not_a_field_any_producer_sends" in envelope["failure_reason"]
        assert "_envelope" not in envelope["failure_reason"]

    def test_judge_verdict_carries_the_same_latent_defect(self) -> None:
        """``delegation-judge-verdict.v1`` has no live traffic; it would fail too."""
        published, capture = _capture()
        runner = DelegationProjectionRunner(publish_fn=capture)
        db = _mock_db()
        db.execute = AsyncMock(side_effect=_judge_attribution)
        runner._db = db

        data = _runner_payload(_judge_verdict_payload(correlation_id=CORRELATION_ID))
        meta = MessageMeta(partition=0, offset=0, fallback_id=CORRELATION_ID)

        ok = asyncio.run(runner.project_event(runner._topic_judge_verdict, data, meta))

        assert ok is True
        dlq = [json.loads(v.decode("utf-8")) for t, v in published if t == DLQ_TOPIC]
        assert not [row for row in dlq if "_envelope" in row["failure_reason"]], (
            "RED before the fix: quarantined on the runner's own transport key"
        )


async def _judge_attribution(query: str, *_args: Any, **_kwargs: Any) -> Any:
    """Answer the OMN-17627 attribution SELECT; every other statement, no rows."""
    if "SELECT tenant_id FROM delegation_events" in query:
        return [{"tenant_id": str(MIRRORED_TENANT_UUID)}]
    return []


@pytest.mark.unit
class TestDefectBTenantIdentityIsKeyedByItsShape:
    """A canonical UUID resolves against ``tenant_uuid``, never ``tenant_slug``."""

    def test_terminal_with_uuid_tenant_writes_the_row(self) -> None:
        _published, capture = _capture()
        runner = DelegationProjectionRunner(publish_fn=capture)
        db = _mock_db(mirror={MIRRORED_TENANT_SLUG: MIRRORED_TENANT_UUID})
        runner._db = db

        topic = runner._topic_delegation_failed
        assert topic, "contract must declare a delegation-failed topic"
        data = _runner_payload(
            _delegation_failed_payload(
                correlation_id=CORRELATION_ID, tenant_id=str(MIRRORED_TENANT_UUID)
            )
        )
        meta = MessageMeta(partition=0, offset=63, fallback_id=CORRELATION_ID)

        ok = asyncio.run(runner.project_event(topic, data, meta))

        assert ok is True
        writes = _delegation_writes(db)
        assert len(writes) == 1, (
            "RED before the fix: TenantRegistryResolutionError -> POISON -> DLQ"
        )
        by_column = _param_by_column(writes[0].args)
        assert by_column["correlation_id"] == CORRELATION_ID
        assert by_column["tenant_id"] == str(MIRRORED_TENANT_UUID)

    def test_the_mirror_is_read_by_the_uuid_column(self) -> None:
        """The predicate itself is the defect; assert which column was keyed."""
        runner = DelegationProjectionRunner()
        db = _mock_db(mirror={MIRRORED_TENANT_SLUG: MIRRORED_TENANT_UUID})
        runner._db = db

        data = _runner_payload(
            _delegation_failed_payload(
                correlation_id=CORRELATION_ID, tenant_id=str(MIRRORED_TENANT_UUID)
            )
        )
        asyncio.run(
            runner.project_event(
                runner._topic_delegation_failed,
                data,
                MessageMeta(partition=0, offset=63, fallback_id=CORRELATION_ID),
            )
        )

        lookups = [str(call.args[0]) for call in db.fetchval.await_args_list]
        assert lookups, "the writer must consult the registry mirror"
        assert all("WHERE tenant_uuid = $1" in sql for sql in lookups), (
            f"a UUID identity must never be matched against tenant_slug: {lookups}"
        )

    def test_an_unmirrored_uuid_still_refuses_and_invents_nothing(self) -> None:
        """No fallback. A UUID the mirror does not hold is not attributable."""
        runner = DelegationProjectionRunner()
        db = _mock_db(mirror={MIRRORED_TENANT_SLUG: MIRRORED_TENANT_UUID})
        runner._db = db

        data = _runner_payload(
            _delegation_failed_payload(
                correlation_id=CORRELATION_ID, tenant_id=str(UNMIRRORED_TENANT_UUID)
            )
        )

        with pytest.raises(TenantRegistryResolutionError) as excinfo:
            asyncio.run(
                runner.project_event(
                    runner._topic_delegation_failed,
                    data,
                    MessageMeta(partition=0, offset=64, fallback_id=CORRELATION_ID),
                )
            )

        assert str(UNMIRRORED_TENANT_UUID) in str(excinfo.value)
        assert not _delegation_writes(db)

    def test_a_slug_identity_still_resolves_by_slug(self) -> None:
        """Monotonic: the pre-existing slug path is unchanged (OMN-16804)."""
        runner = DelegationProjectionRunner()
        db = _mock_db(mirror={MIRRORED_TENANT_SLUG: MIRRORED_TENANT_UUID})
        runner._db = db

        data = _runner_payload(
            _delegation_failed_payload(
                correlation_id=CORRELATION_ID, tenant_id=MIRRORED_TENANT_SLUG
            )
        )
        ok = asyncio.run(
            runner.project_event(
                runner._topic_delegation_failed,
                data,
                MessageMeta(partition=0, offset=65, fallback_id=CORRELATION_ID),
            )
        )

        assert ok is True
        writes = _delegation_writes(db)
        assert len(writes) == 1
        assert _param_by_column(writes[0].args)["tenant_id"] == str(
            MIRRORED_TENANT_UUID
        )
        lookups = [str(call.args[0]) for call in db.fetchval.await_args_list]
        assert all("WHERE tenant_slug = $1" in sql for sql in lookups)


@pytest.mark.unit
class TestBothDefectsMustBeFixedTogether:
    """Neither fix alone produces a row for the onex-dev delegation."""

    def test_a_whole_delegation_both_events_reach_the_same_row(self) -> None:
        published, capture = _capture()
        runner = DelegationProjectionRunner(publish_fn=capture)
        db = _mock_db(mirror={MIRRORED_TENANT_SLUG: MIRRORED_TENANT_UUID})
        runner._db = db

        # The exact supply the two onex-dev delegations put on the bus: one
        # quality-gate-result, then one delegation-failed, nothing else.
        asyncio.run(
            runner.project_event(
                runner._topic_quality_gate_result,
                _runner_payload(_quality_gate_payload(correlation_id=CORRELATION_ID)),
                MessageMeta(partition=0, offset=216, fallback_id=CORRELATION_ID),
            )
        )
        asyncio.run(
            runner.project_event(
                runner._topic_delegation_failed,
                _runner_payload(
                    _delegation_failed_payload(
                        correlation_id=CORRELATION_ID,
                        tenant_id=str(MIRRORED_TENANT_UUID),
                    )
                ),
                MessageMeta(partition=0, offset=63, fallback_id=CORRELATION_ID),
            )
        )

        assert not [t for t, _ in published if t == DLQ_TOPIC]
        writes = _delegation_writes(db)
        assert len(writes) == 2, "both source events reach the write path"
        assert {
            str(_param_by_column(write.args)["correlation_id"]) for write in writes
        } == {CORRELATION_ID}
