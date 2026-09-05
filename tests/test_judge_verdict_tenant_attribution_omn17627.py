# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17627: judge-verdict rows must not invent DEFAULT_TENANT.

``_resolve_judge_verdict_tenant_id`` joins a judge verdict to
``delegation_events`` by correlation_id and returns ``DEFAULT_TENANT``
("omninode") whenever that join is missing, blank, or malformed. The ratified
OMN-16831/OMN-16804 rule is that tenant attribution is producer-recorded or
verified, never invented -- so a row nobody can attribute must refuse the
write, not silently claim the house tenant.

This inverts a deliberate pin from OMN-14894 tranche 2
(``test_judge_verdict_falls_back_to_default_tenant_when_unmatched``), which
required the default precisely so the row was "never silently tenant-less".
Both rulings are satisfied by refusing rather than defaulting: the async
writer routes the refused event to the contract-declared DLQ, so the verdict
is durably recoverable on the bus instead of being either invented or lost.

On the multiple-match case: ``delegation_events.correlation_id`` is
``NOT NULL UNIQUE`` (migration 0007, lines 11 and 138), so two rows sharing a
correlation_id cannot exist on the real store. That guard is DEFENSIVE and its
test seeds the in-memory double directly; it documents a refusal, and is not
evidence of a reachable live defect.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.events.delegation_judge_verdict import (
    EnumDelegationJudgeVerdict,
    build_delegation_judge_verdict_event,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    JUDGE_VERDICT_TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE as DELEGATION_TABLE,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.tenant_isolation import TenantRequiredError


def _verdict(correlation_id: object) -> object:
    return build_delegation_judge_verdict_event(
        correlation_id=correlation_id,  # type: ignore[arg-type]
        task_type="research",
        judge_model="glm-5.2",
        judge_model_version="v1",
        judge_provider="zai",
        rubric_id="rubric-1",
        rubric_hash="sha256:" + "a" * 64,
        prompt="prompt text",
        judged_input="input text",
        temperature=0.0,
        judge_node_version="1.0.0",
        reasoning="reasoning text",
        verdict=EnumDelegationJudgeVerdict.PASS,
        actual_score=0.9,
    )


@pytest.mark.unit
def test_verified_attribution_is_used_ac1() -> None:
    """AC1: a non-blank tenant on the matching delegation row is adopted."""
    db = InmemoryDatabaseAdapter()
    correlation_id = uuid4()
    db.upsert(
        DELEGATION_TABLE,
        "correlation_id",
        {"correlation_id": str(correlation_id), "tenant_id": "tenant-a"},
    )

    handler = HandlerProjectionDelegation()
    result = handler.project_judge_verdict(_verdict(correlation_id), db)  # type: ignore[arg-type]

    assert result.rows_upserted == 1
    assert db.query(JUDGE_VERDICT_TABLE)[0]["tenant_id"] == "tenant-a"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stored_row", "case"),
    [
        (None, "no matching delegation_events row (late/out-of-order)"),
        ({}, "matching row carries no tenant_id column at all"),
        ({"tenant_id": ""}, "empty tenant_id"),
        ({"tenant_id": "   "}, "whitespace-only tenant_id"),
        ({"tenant_id": 12345}, "malformed tenant_id: int, not str"),
        ({"tenant_id": None}, "malformed tenant_id: explicit NULL"),
        ({"tenant_id": ["tenant-a"]}, "malformed tenant_id: list"),
    ],
)
def test_unattributable_verdict_refuses_the_write_ac2(
    stored_row: dict[str, object] | None, case: str
) -> None:
    """AC2: every unattributable shape raises and writes NO row.

    The house default must not absorb the write. Asserting the empty table is
    the load-bearing half -- a raise that still left a DEFAULT_TENANT row
    behind would satisfy the exception check and violate the actual rule.
    """
    db = InmemoryDatabaseAdapter()
    correlation_id = uuid4()
    if stored_row is not None:
        db.upsert(
            DELEGATION_TABLE,
            "correlation_id",
            {"correlation_id": str(correlation_id), **stored_row},
        )

    handler = HandlerProjectionDelegation()
    with pytest.raises(TenantRequiredError):
        handler.project_judge_verdict(_verdict(correlation_id), db)  # type: ignore[arg-type]

    assert db.query(JUDGE_VERDICT_TABLE) == [], (
        f"{case}: refused write still produced a row -- the column default absorbed it"
    )


@pytest.mark.unit
def test_conflicting_attribution_refuses_defensively_ac2() -> None:
    """AC2's multiple-match shape: refuse rather than silently take matches[0].

    UNREACHABLE on the real store -- delegation_events.correlation_id is
    NOT NULL UNIQUE (migration 0007) -- so this seeds the in-memory double
    directly and proves a defensive refusal, not a live defect.
    """
    db = InmemoryDatabaseAdapter()
    correlation_id = uuid4()
    db.tables[DELEGATION_TABLE] = [
        {"correlation_id": str(correlation_id), "tenant_id": "tenant-a"},
        {"correlation_id": str(correlation_id), "tenant_id": "tenant-b"},
    ]

    handler = HandlerProjectionDelegation()
    with pytest.raises(TenantRequiredError):
        handler.project_judge_verdict(_verdict(correlation_id), db)  # type: ignore[arg-type]

    assert db.query(JUDGE_VERDICT_TABLE) == []


@pytest.mark.unit
def test_no_house_default_is_consulted_ac3() -> None:
    """AC3: the refusal path never reads DEFAULT_TENANT.

    Pins the rule by outcome rather than by inspection: if any fallback were
    still consulted the write would succeed, so a passing raise IS the proof
    that no configured default answered.
    """
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionDelegation()

    with pytest.raises(TenantRequiredError) as excinfo:
        handler.project_judge_verdict(_verdict(uuid4()), db)  # type: ignore[arg-type]

    assert "omninode" not in str(excinfo.value), (
        "refusal message names the house tenant, implying it was resolved"
    )
    assert db.query(JUDGE_VERDICT_TABLE) == []


# ---------------------------------------------------------------------------
# The async writer -- the path that actually runs on the lane.
#
# Before OMN-17627 this writer had NO test of any kind: its INSERT named 18
# columns, tenant_id was not among them, and Postgres' DEFAULT 'omninode'
# attributed every row it wrote. Adding a required join broke nothing in the
# suite, which is itself the finding -- an unwatched writer.
# ---------------------------------------------------------------------------


class _RecordingDb:
    """Minimal async double: answers the attribution SELECT, records INSERTs."""

    def __init__(self, attribution_rows: list[dict[str, object]]) -> None:
        self._attribution_rows = attribution_rows
        self.inserts: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, sql: str, *args: object, **_: object) -> object:
        if sql.lstrip().upper().startswith("SELECT"):
            return self._attribution_rows
        self.inserts.append((sql, args))
        return None


def _runner_with(attribution_rows: list[dict[str, object]]) -> object:
    from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
        DelegationProjectionRunner,
    )

    runner = DelegationProjectionRunner()
    runner._db = _RecordingDb(attribution_rows)  # type: ignore[attr-defined]
    runner._dlq_calls = []  # type: ignore[attr-defined]

    async def _capture(
        data: dict[str, object], reason: str, meta: object = None
    ) -> bool:
        runner._dlq_calls.append(reason)  # type: ignore[attr-defined]
        return True

    runner._route_malformed_to_dlq = _capture  # type: ignore[assignment]
    return runner


@pytest.mark.unit
async def test_async_writer_records_the_producer_tenant() -> None:
    """The async INSERT now carries tenant_id, sourced from the join."""
    correlation_id = uuid4()
    runner = _runner_with([{"tenant_id": "tenant-a"}])
    payload = _verdict(correlation_id).model_dump(mode="json")  # type: ignore[attr-defined]

    assert await runner._project_judge_verdict(payload) is True  # type: ignore[attr-defined]

    assert runner._dlq_calls == []  # type: ignore[attr-defined]
    inserts = runner._db.inserts  # type: ignore[attr-defined]
    assert len(inserts) == 1
    sql, args = inserts[0]
    assert "tenant_id" in sql, "writer still omits the tenant_id column"
    assert args[-1] == "tenant-a", "tenant_id is not the producer-recorded value"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("attribution_rows", "case"),
    [
        ([], "no delegation row joins this correlation_id"),
        ([{"tenant_id": ""}], "blank tenant on the joined row"),
        ([{"tenant_id": None}], "NULL tenant on the joined row"),
        ([{"other": "col"}], "joined row carries no tenant_id at all"),
        (
            [{"tenant_id": "tenant-a"}, {"tenant_id": "tenant-b"}],
            "conflicting attributions (defensive; UNIQUE makes this unreachable)",
        ),
    ],
)
async def test_async_writer_dlqs_instead_of_defaulting(
    attribution_rows: list[dict[str, object]], case: str
) -> None:
    """Unattributable -> DLQ, and crucially NO row is written.

    Asserting the empty insert list is the load-bearing half: routing to the
    DLQ while still writing a house-stamped row would satisfy a naive DLQ
    assertion and leave the defect exactly where it was.
    """
    runner = _runner_with(attribution_rows)
    payload = _verdict(uuid4()).model_dump(mode="json")  # type: ignore[attr-defined]

    assert await runner._project_judge_verdict(payload) is True  # type: ignore[attr-defined]

    assert runner._db.inserts == [], f"{case}: refused verdict still wrote a row"  # type: ignore[attr-defined]
    assert len(runner._dlq_calls) == 1, f"{case}: not routed to the DLQ"  # type: ignore[attr-defined]
    assert "OMN-17627" in runner._dlq_calls[0]  # type: ignore[attr-defined]
