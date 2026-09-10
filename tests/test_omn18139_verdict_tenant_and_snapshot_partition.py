# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18139: the quality verdict refuses an unattributable write, and the
tenant partition survives the snapshot boundary.

TWO DEFECTS, ONE CAUSE. Both are the delegation writer answering a tenant
question with a value nobody recorded.

DEFECT 1 -- THE VERDICT STAMPED THE HOUSE TENANT. A quality verdict carries no
tenant: ``ModelQualityGateResult`` is ``extra="forbid"`` and has no tenant
field, and the producer records none on the envelope, so
``envelope_tenant_identity`` returns ``None`` for every verdict on the lane.
The path then fell back to the house tenant.

Measured on onex-dev 2026-09-10, and this is what turned the staging
business-proof gate red on ``quality_gate`` for five consecutive runs: the
verdict wrote ``delegation_events`` under the house tenant
``820272f9-4aaf-5add-a2df-0af942852ab2`` while the reader queried the
submitting tenant resolved from its own API key,
``91c74442-1233-4c97-b191-911a10346fdf``. The row existed and the reader
structurally could not see it.

It also blocked the terminal behind it, which is the half that makes this
worse than a mislabelled row. The verdict usually arrives ~1.5s BEFORE its
terminal, so it CREATES the row; the ``delegation-completed`` event then
upserts the same ``correlation_id`` under the submitting tenant, and its
``ON CONFLICT DO UPDATE`` arm must read the existing row through the policy's
USING clause -- which refuses, because that row belongs to another tenant.
That is the ``new row violates row-level security policy (USING expression)``
refusal measured on the same correlation.

WHY IT REFUSES RATHER THAN RESOLVING BY CORRELATION. The judge-verdict path
(OMN-17627) recovers attribution by joining ``delegation_events`` on
``correlation_id``. That mechanism cannot be borrowed here: under FORCE ROW
LEVEL SECURITY the probe must already be bound to the right tenant to see the
row, so it can only CONFIRM a tenant it was told, never DISCOVER one. A verdict
with no recorded tenant is unattributable to this writer. Refusing loses no
column, because the terminal carries the same quality-gate fields and writes
them under the tenant that actually submitted the work.

DEFECT 2 -- THE PARTITION DIED AT THE SNAPSHOT BOUNDARY. OMN-18139 made the
aggregate re-read tenant-scoped. ``encode_snapshot_delta`` derives the compacted
message key from the exposure's declared ``key_columns``, and these four
aggregates declared only the constant ``snapshot_grain``; ``publish_snapshot_delta``
also defaults its header ``tenant_id`` to the house slug. So a tenant-scoped
number was about to be published under a tenant-agnostic key with a house-tenant
header: last writer wins, and a reader is served another tenant's figures.
Before the read fix this could not happen, because the read aborted and nothing
published at all -- so carrying the scope only as far as the SQL would have
traded a loud failure for a silent cross-tenant one. Found in review on
omnimarket#2445 before it could reach a lane.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    SNAPSHOT_AGGREGATE_KEY,
    SNAPSHOT_GRAIN_COLUMN,
    SNAPSHOT_TENANT_COLUMN,
    DelegationProjectionRunner,
)
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_UUID

#: The submitting tenant, as resolved from an API key on the staging lane. A
#: v4, deliberately unlike the derived house identity.
_SUBMITTING_TENANT = "91c74442-1233-4c97-b191-911a10346fdf"


async def _capture(topic: str, value: bytes) -> None:
    return


def _verdict(*, correlation_id: str, tenant_id: str | None) -> dict[str, Any]:
    """A quality-gate-result delivery, with or without a recorded tenant."""
    envelope: dict[str, Any] = {
        "payload": {
            "correlation_id": correlation_id,
            "passed": True,
            "fail_category": "pass",
            "quality_score": 1.0,
            "failure_reasons": [],
            "fallback_recommended": False,
            "score_source": "deterministic_acceptance",
            "actual_score": 1.0,
        },
        "envelope_id": str(uuid4()),
        "correlation_id": correlation_id,
        "event_type": "omnibase-infra.quality-gate-result",
        "envelope_timestamp": "2026-09-10T10:21:18.312000+00:00",
    }
    if tenant_id is not None:
        envelope["tenant_id"] = tenant_id
    unwrapped = unwrap_envelope(json.dumps(envelope).encode("utf-8"))
    assert unwrapped is not None
    return unwrapped


def _runner() -> DelegationProjectionRunner:
    runner = DelegationProjectionRunner(publish_fn=_capture)
    runner._db = AsyncMock()  # type: ignore[assignment]
    runner._db.execute = AsyncMock(return_value=[])
    return runner


class TestTheVerdictRefusesAnUnattributableWrite:
    """RED before OMN-18139's second change: the verdict stamped
    ``HOUSE_TENANT_UUID`` and wrote the row."""

    @pytest.mark.asyncio
    async def test_a_verdict_with_no_recorded_tenant_is_routed_to_the_dlq(
        self,
    ) -> None:
        runner = _runner()
        routed: list[str] = []

        async def _dlq(data: dict[str, Any], reason: str, meta: Any) -> bool:
            routed.append(reason)
            return True

        runner._route_malformed_to_dlq = _dlq  # type: ignore[assignment]
        correlation_id = str(uuid4())

        await runner._project_quality_gate_result(
            _verdict(correlation_id=correlation_id, tenant_id=None),
            MessageMeta(partition=0, offset=266, fallback_id=correlation_id),
        )

        assert len(routed) == 1, (
            "an unattributable verdict was not refused -- it fell through to a "
            "write, which is the house-tenant stamp this change removes"
        )
        assert "tenant attribution unresolved" in routed[0]
        assert correlation_id in routed[0], (
            "the refusal must name the correlation so the record is "
            "recoverable rather than merely rejected"
        )

    @pytest.mark.asyncio
    async def test_no_write_is_issued_for_an_unattributable_verdict(self) -> None:
        """The refusal must precede any SQL. A row written and then regretted
        is the exact state that blocks the terminal behind a USING refusal."""
        runner = _runner()
        runner._route_malformed_to_dlq = AsyncMock(return_value=True)  # type: ignore[assignment]
        upserts: list[dict[str, Any]] = []

        async def _upsert(**kwargs: Any) -> None:
            upserts.append(kwargs)

        runner._dynamic_upsert = _upsert  # type: ignore[assignment]
        correlation_id = str(uuid4())

        await runner._project_quality_gate_result(
            _verdict(correlation_id=correlation_id, tenant_id=None),
            MessageMeta(partition=0, offset=266, fallback_id=correlation_id),
        )

        assert upserts == [], (
            "the verdict issued a write for an event whose tenant nobody recorded"
        )

    @pytest.mark.asyncio
    async def test_the_house_tenant_is_never_stamped_by_this_path(self) -> None:
        """The property stated directly, so a future fallback reintroduced by
        any route is red -- not only the one this change removed."""
        runner = _runner()
        runner._route_malformed_to_dlq = AsyncMock(return_value=True)  # type: ignore[assignment]
        seen: list[str] = []

        async def _upsert(**kwargs: Any) -> None:
            row = kwargs.get("row") or {}
            seen.append(str(row.get("tenant_id")))

        runner._dynamic_upsert = _upsert  # type: ignore[assignment]

        await runner._project_quality_gate_result(
            _verdict(correlation_id=str(uuid4()), tenant_id=None),
            MessageMeta(partition=0, offset=266, fallback_id="f"),
        )

        assert str(HOUSE_TENANT_UUID) not in seen, (
            f"the verdict stamped the house tenant {HOUSE_TENANT_UUID}, the "
            "identity that made the staging reader blind to its own row"
        )


class TestTheRecordedTenantIsStillHonoured:
    """POSITIVE CONTROL for the whole refusal above. If a verdict that DOES
    carry a tenant were also refused, the tests above would pass for the wrong
    reason -- a path that refuses everything is not a path that refuses the
    unattributable."""

    @pytest.mark.asyncio
    async def test_a_verdict_carrying_a_tenant_is_written_under_it(self) -> None:
        runner = _runner()

        async def _resolve(identity: str | None) -> str | None:
            return _SUBMITTING_TENANT if identity else None

        runner._resolve_write_tenant_uuid = _resolve  # type: ignore[assignment]
        runner._route_malformed_to_dlq = AsyncMock(return_value=True)  # type: ignore[assignment]
        seen: list[dict[str, Any]] = []

        async def _upsert(**kwargs: Any) -> None:
            seen.append(kwargs)

        runner._dynamic_upsert = _upsert  # type: ignore[assignment]

        await runner._project_quality_gate_result(
            _verdict(correlation_id=str(uuid4()), tenant_id=_SUBMITTING_TENANT),
            MessageMeta(partition=0, offset=266, fallback_id="f"),
        )

        assert len(seen) == 1, "an attributable verdict was not written"
        assert seen[0]["tenant"] == _SUBMITTING_TENANT
        assert seen[0]["row"]["tenant_id"] == _SUBMITTING_TENANT


class TestTheTenantPartitionSurvivesTheSnapshotBoundary:
    """RED before this change: the aggregate published under a key of grain
    alone, with a house-slug header."""

    def test_the_declared_aggregate_key_carries_the_tenant(self) -> None:
        assert SNAPSHOT_AGGREGATE_KEY == (
            SNAPSHOT_GRAIN_COLUMN,
            SNAPSHOT_TENANT_COLUMN,
        ), (
            "the aggregate compaction key no longer names the tenant, so every "
            "tenant's aggregate compacts onto one record and the last writer's "
            "numbers are served to all of them"
        )

    def test_every_bus_backed_aggregate_exposure_declares_that_key(self) -> None:
        """The contract and the code must agree, because the key is derived
        from the CONTRACT at publish time and built by the CODE's SELECT. A
        disagreement is a message whose key names a column the row lacks."""
        runner = _runner()
        exposures = runner._aggregate_exposures
        assert exposures, (
            "no bus_backed aggregate exposures were resolved -- the assertion "
            "below would be vacuous"
        )
        for exposure in exposures:
            assert tuple(exposure.key_columns) == SNAPSHOT_AGGREGATE_KEY, (
                f"{exposure.topic} declares key_columns {list(exposure.key_columns)!r}"
            )

    @pytest.mark.asyncio
    async def test_the_republish_selects_the_tenant_and_stamps_the_header(
        self,
    ) -> None:
        runner = _runner()
        tenant = _SUBMITTING_TENANT
        statements: list[tuple[str, tuple[Any, ...]]] = []

        async def _execute(sql: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            statements.append((sql, args))
            return [{SNAPSHOT_GRAIN_COLUMN: "t", SNAPSHOT_TENANT_COLUMN: tenant}]

        runner._db.execute = _execute  # type: ignore[assignment]
        published: list[dict[str, Any]] = []

        async def _publish(exposure: Any, **kwargs: Any) -> bool:
            published.append(kwargs)
            return True

        runner.publish_snapshot_delta = _publish  # type: ignore[assignment]

        await runner._publish_aggregate_snapshots(
            MessageMeta(partition=0, offset=1, fallback_id="f"), tenant=tenant
        )

        assert statements, "the republish issued no statement"
        for sql, args in statements:
            assert SNAPSHOT_TENANT_COLUMN in sql, (
                "the re-read does not select the tenant as a column, so the "
                "row cannot supply the tenant half of the compaction key"
            )
            assert tenant in args, "the tenant was not bound as a parameter"

        assert published, "nothing was published"
        for call in published:
            assert call["tenant_id"] == tenant, (
                "the snapshot header carries the parameter's house-slug "
                "default while the row it describes belongs to another tenant"
            )
            assert call["row"][SNAPSHOT_TENANT_COLUMN] == tenant
