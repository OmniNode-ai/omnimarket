# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bounded event read and exact tenant-key parity for OMN-15359 AC3.

The broker read never joins or commits a consumer group. It captures each
partition's retained start and a fixed end, and refuses an incomplete read.
The comparison refuses empty, duplicate, foreign-tenant and wrong-key results;
row counts alone cannot make a substituted correlation look correct.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from aiokafka import TopicPartition

from omnimarket.events.topics import (
    DELEGATION_COMPLETED_TOPIC_V1,
    DELEGATION_FAILED_TOPIC_V1,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.envelope import envelope_tenant_identity, unwrap_envelope
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter


class ReplayError(RuntimeError):
    """AC3 cannot be claimed from this read or comparison."""


@dataclass(frozen=True)
class OffsetSpan:
    topic: str
    partition: int
    start: int
    end: int
    final: int


@dataclass(frozen=True)
class CapturedRecord:
    topic: str
    partition: int
    offset: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class TenantKeyComparison:
    tenant_id: str
    live_count: int
    replay_count: int
    live_key_sha256: str
    replay_key_sha256: str


def contract_replay_topics(contract_path: Path) -> tuple[str, str]:
    """Take the terminal pair from the owning contract, refusing drift."""
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    subscribed = contract.get("event_bus", {}).get("subscribe_topics", [])
    terminals = (DELEGATION_COMPLETED_TOPIC_V1, DELEGATION_FAILED_TOPIC_V1)
    if any(subscribed.count(topic) != 1 for topic in terminals):
        raise ReplayError("owning contract does not subscribe to both terminal topics")
    return terminals


def build_receipt(
    *,
    lane: str,
    writer_image: str,
    handler_sha256: str,
    probe_sha256: str,
    source_relation: str,
    replay_relation: str,
    empty_target_rows: int,
    source_topics: tuple[str, ...],
    spans: list[OffsetSpan],
    consumed_records: int,
    applied_records: Mapping[str, int],
    comparison: Mapping[str, TenantKeyComparison],
    snapshots_suppressed: int,
) -> dict[str, object]:
    """Emit a green receipt only for a complete, non-empty exact comparison."""
    if not all((lane, writer_image, handler_sha256, probe_sha256)):
        raise ReplayError("receipt lacks lane or source-version identity")
    if re.search(r"sha256:[0-9a-f]{64}(?:$|[^0-9a-f])", writer_image) is None:
        raise ReplayError("writer image lacks a measured sha256 digest")
    if any(
        re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for digest in (handler_sha256, probe_sha256)
    ):
        raise ReplayError("handler or probe digest is malformed")
    if not source_relation or not replay_relation or source_relation == replay_relation:
        raise ReplayError("source and replay relation identities are absent or equal")
    if empty_target_rows != 0:
        raise ReplayError("replay target was not empty")
    if (
        not spans
        or set(source_topics) != {span.topic for span in spans}
        or len({(span.topic, span.partition) for span in spans}) != len(spans)
        or any(
            span.start < 0 or span.end < span.start or span.final < span.end
            for span in spans
        )
    ):
        raise ReplayError("receipt has an incomplete offset vector")
    if consumed_records < 1 or set(applied_records) != set(comparison):
        raise ReplayError("receipt lacks synthetic records or tenant counts")
    for tenant, item in comparison.items():
        if (
            item.tenant_id != tenant
            or item.live_count < 1
            or item.replay_count != item.live_count
            or item.live_key_sha256 != item.replay_key_sha256
            or applied_records[tenant] < item.replay_count
        ):
            raise ReplayError(f"receipt cannot attest exact key parity for {tenant}")
    receipt: dict[str, object] = {
        "ticket": "OMN-15359",
        "criterion": "AC3",
        "result": "PASS",
        "lane": lane,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "writer_image": writer_image,
        "deployed_handler_sha256": handler_sha256,
        "probe_sha256": probe_sha256,
        "source_relation": source_relation,
        "replay_relation": replay_relation,
        "empty_target_rows_before": empty_target_rows,
        "offsets": [span.__dict__ for span in spans],
        "source_topics": list(source_topics),
        "consumed_records": consumed_records,
        "applied_records": dict(applied_records),
        "snapshots_suppressed": snapshots_suppressed,
        "tenants": {tenant: item.__dict__ for tenant, item in comparison.items()},
        "scope": "per-tenant row counts and sorted (tenant_id, correlation_id) keys only",
    }
    return receipt


def _keys(
    rows: Iterable[Mapping[str, object]], expected_tenants: frozenset[str]
) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        tenant, correlation = row.get("tenant_id"), row.get("correlation_id")
        if not isinstance(tenant, str) or tenant not in expected_tenants:
            raise ReplayError(f"row has foreign or absent tenant: {tenant!r}")
        if not isinstance(correlation, str) or not correlation:
            raise ReplayError("row has absent correlation_id")
        grouped[tenant].append(correlation)
    return grouped


def _key_digest(tenant: str, correlations: set[str]) -> str:
    canonical = json.dumps(
        [(tenant, correlation) for correlation in sorted(correlations)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def compare_tenant_keys(
    expected: Mapping[str, set[str]],
    *,
    live_rows: Iterable[Mapping[str, object]],
    replay_rows: Iterable[Mapping[str, object]],
) -> dict[str, TenantKeyComparison]:
    """Require exact, non-empty correlation sets for two synthetic tenants."""
    if len(expected) != 2 or any(
        not tenant or not keys for tenant, keys in expected.items()
    ):
        raise ReplayError("AC3 requires exactly two non-empty synthetic tenants")
    if any(not key for keys in expected.values() for key in keys):
        raise ReplayError("expected correlation_id is blank")
    tenants = frozenset(expected)
    live = _keys(live_rows, tenants)
    replay = _keys(replay_rows, tenants)
    result: dict[str, TenantKeyComparison] = {}
    for tenant, wanted in expected.items():
        live_keys, replay_keys = live[tenant], replay[tenant]
        if len(live_keys) != len(set(live_keys)):
            raise ReplayError(f"live rows duplicate a correlation for {tenant}")
        if len(replay_keys) != len(set(replay_keys)):
            raise ReplayError(f"replay rows duplicate a correlation for {tenant}")
        if set(live_keys) != wanted or set(replay_keys) != wanted:
            raise ReplayError(
                f"AC3 key mismatch for {tenant}: expected={sorted(wanted)!r}, "
                f"live={sorted(live_keys)!r}, replay={sorted(replay_keys)!r}"
            )
        live_digest = _key_digest(tenant, set(live_keys))
        replay_digest = _key_digest(tenant, set(replay_keys))
        if live_digest != replay_digest:
            raise ReplayError(f"AC3 key digest mismatch for {tenant}")
        result[tenant] = TenantKeyComparison(
            tenant_id=tenant,
            live_count=len(live_keys),
            replay_count=len(replay_keys),
            live_key_sha256=live_digest,
            replay_key_sha256=replay_digest,
        )
    return result


def replay_synthetic_records(
    records: Iterable[CapturedRecord],
    expected: Mapping[str, set[str]],
    db: PostgresSyncProjectionAdapter,
    *,
    handler: HandlerProjectionDelegation,
) -> dict[str, int]:
    """Feed only this run's records through the deployed projection handler.

    The target is required empty. Envelope conversion is the production
    ``unwrap_envelope`` seam; the caller injects an owned database adapter and
    a handler with a recording publisher so replay cannot publish to the bus.
    """
    # A tenant-scoped query can return zero under RLS while rows for other
    # tenants exist. Require a global, unfiltered count with RLS inactive.
    conn = db._connect()  # noqa: SLF001 - same adapter connection, global RLS-safe count
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT row_security_active('delegation_events'::regclass)")
            if cur.fetchone()[0]:
                raise ReplayError("replay target's RLS hides global emptiness")
            cur.execute("SELECT COUNT(*) FROM delegation_events")
            if cur.fetchone()[0] != 0:
                raise ReplayError("replay target was not empty before the first event")
    finally:
        conn.close()
    if len(expected) != 2 or any(not keys for keys in expected.values()):
        raise ReplayError("AC3 requires two non-empty synthetic tenant fixtures")
    owner_by_correlation: dict[str, str] = {}
    for tenant, keys in expected.items():
        for correlation in keys:
            if correlation in owner_by_correlation:
                raise ReplayError("synthetic correlation belongs to two tenants")
            owner_by_correlation[correlation] = tenant
    selected: list[tuple[CapturedRecord, dict[str, object], str]] = []
    seen: set[str] = set()
    allowed_topics = {DELEGATION_COMPLETED_TOPIC_V1, DELEGATION_FAILED_TOPIC_V1}
    for record in records:
        if record.topic not in allowed_topics:
            raise ReplayError(f"non-contract replay topic: {record.topic}")
        decoded = unwrap_envelope(json.dumps(record.payload).encode("utf-8"))
        if decoded is None:
            raise ReplayError(
                f"unreadable envelope at {record.topic}/{record.partition}/{record.offset}"
            )
        decoded_correlation = decoded.get("correlation_id")
        if (
            not isinstance(decoded_correlation, str)
            or decoded_correlation not in owner_by_correlation
        ):
            continue
        tenant = owner_by_correlation[decoded_correlation]
        payload_tenant = decoded.get("tenant_id")
        envelope_tenant = envelope_tenant_identity(decoded)
        if payload_tenant != tenant or (
            envelope_tenant is not None and envelope_tenant != tenant
        ):
            raise ReplayError(
                f"synthetic correlation {decoded_correlation} names a different tenant"
            )
        selected.append((record, decoded, tenant))
        seen.add(decoded_correlation)
    missing = set(owner_by_correlation) - seen
    if missing:
        raise ReplayError(
            f"synthetic records absent from retained window: {sorted(missing)!r}"
        )
    counts = dict.fromkeys(expected, 0)
    for record, decoded, tenant in selected:
        input_data: dict[str, object] = dict(decoded)
        input_data.update(
            {
                "_db": db,
                "_event_type": record.topic,
                "_topic": record.topic,
                "_tenant_id": tenant,
            }
        )
        handler.handle(input_data)
        counts[tenant] += 1
    return counts


async def capture_retained(
    consumer_factory: Callable[..., Any],
    topics: tuple[str, ...],
    *,
    max_idle_polls: int = 6,
    max_records: int = 200_000,
    max_seconds: float = 300.0,
) -> tuple[list[CapturedRecord], list[OffsetSpan]]:
    """Read every assigned partition from earliest retained to fixed end.

    ``consumer_factory`` is the configured aiokafka consumer constructor. A
    fresh consumer with no group id cannot advance a deployed writer's offset.
    The caller owns credential resolution and supplies only the contract topics.
    """
    if not topics or len(topics) != len(set(topics)):
        raise ReplayError("topics must be a non-empty unique contract set")
    if max_idle_polls < 1 or max_records < 1 or max_seconds <= 0:
        raise ReplayError("read bounds must be positive")
    consumer = consumer_factory(
        group_id=None,
        enable_auto_commit=False,
        auto_offset_reset="none",
    )
    deadline = time.monotonic() + max_seconds
    records: list[CapturedRecord] = []
    try:
        await asyncio.wait_for(consumer.start(), timeout=30)
        discovered: list[TopicPartition] = []
        for topic in topics:
            partition_ids = consumer.partitions_for_topic(topic)
            if not partition_ids:
                raise ReplayError(f"contract topic has no broker partitions: {topic}")
            discovered.extend(
                TopicPartition(topic, partition) for partition in sorted(partition_ids)
            )
        discovered.sort(key=lambda tp: (tp.topic, tp.partition))
        consumer.assign(discovered)
        partitions = sorted(
            consumer.assignment(), key=lambda tp: (tp.topic, tp.partition)
        )
        if partitions != discovered:
            raise ReplayError("broker assignment omitted a discovered partition")
        starts = await asyncio.wait_for(
            consumer.beginning_offsets(partitions), timeout=30
        )
        ends = await asyncio.wait_for(consumer.end_offsets(partitions), timeout=30)
        for tp in partitions:
            if tp not in starts or tp not in ends:
                raise ReplayError(f"missing offset bound for {tp.topic}/{tp.partition}")
            start, end = starts[tp], ends[tp]
            if (
                not isinstance(start, int)
                or not isinstance(end, int)
                or start < 0
                or end < start
            ):
                raise ReplayError(f"invalid offset bound for {tp.topic}/{tp.partition}")
            consumer.seek(tp, start)
        pending = {tp for tp in partitions if starts[tp] < ends[tp]}
        idle_polls = 0
        while pending:
            if time.monotonic() >= deadline:
                raise ReplayError("retained read exceeded its time bound")
            batch = await asyncio.wait_for(
                consumer.getmany(
                    *sorted(pending, key=lambda tp: (tp.topic, tp.partition)),
                    timeout_ms=5000,
                ),
                timeout=10,
            )
            progressed = False
            for tp in list(pending):
                for message in batch.get(tp, []):
                    if message.topic != tp.topic or message.partition != tp.partition:
                        raise ReplayError(
                            "broker record partition differs from assignment"
                        )
                    if message.offset < starts[tp] or message.offset >= ends[tp]:
                        continue
                    try:
                        payload = json.loads(message.value)
                    except (TypeError, ValueError, UnicodeDecodeError) as exc:
                        raise ReplayError(
                            f"undecodable record at {tp.topic}/{tp.partition}/{message.offset}"
                        ) from exc
                    if not isinstance(payload, dict):
                        raise ReplayError(
                            f"non-object record at {tp.topic}/{tp.partition}/{message.offset}"
                        )
                    records.append(
                        CapturedRecord(tp.topic, tp.partition, message.offset, payload)
                    )
                    if len(records) > max_records:
                        raise ReplayError("retained read exceeded its record bound")
                    progressed = True
                if (
                    await asyncio.wait_for(consumer.position(tp), timeout=10)
                    >= ends[tp]
                ):
                    pending.remove(tp)
                    progressed = True
            idle_polls = 0 if progressed else idle_polls + 1
            if idle_polls >= max_idle_polls:
                raise ReplayError("retained read stalled before its fixed end offsets")
        spans = [
            OffsetSpan(
                tp.topic,
                tp.partition,
                starts[tp],
                ends[tp],
                await asyncio.wait_for(consumer.position(tp), timeout=10),
            )
            for tp in partitions
        ]
        if any(span.final < span.end for span in spans):
            raise ReplayError("retained read stopped before a fixed end offset")
        return records, spans
    except ReplayError:
        raise
    except (Exception, asyncio.CancelledError) as exc:
        raise ReplayError(
            f"retained broker read failed: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        try:
            await asyncio.wait_for(consumer.stop(), timeout=30)
        except Exception:
            # A failed stop cannot turn a refused read into a green receipt.
            raise ReplayError("broker consumer did not stop cleanly") from None
