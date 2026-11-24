#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15359 AC3: publish fixtures and replay into a disposable empty schema.

Each live action is explicit: publish the bounded two-tenant fixture, provision
one new disposable schema, or replay retained records into that schema.  The
program never truncates or writes the live projection table. Dry-run needs no
credentials and never opens a database or broker connection.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.topics import (
    DELEGATION_COMPLETED_TOPIC_V1,
    DELEGATION_FAILED_TOPIC_V1,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.ac3_replay import (
    ReplayError,
    build_receipt,
    capture_retained,
    compare_tenant_keys,
    contract_replay_topics,
    replay_synthetic_records,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.topic_namespace import apply_topic_namespace_all

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
HANDLER = (
    ROOT
    / "src/omnimarket/nodes/node_projection_delegation/handlers/handler_projection_delegation.py"
)
MODULE = ROOT / "src/omnimarket/projection/ac3_replay.py"
_LANE_ENVIRONMENTS = {
    "operator-201": "lakshman",
    "shared-dev-201": "dev",
}
_REPLAY_SCHEMA_RE = re.compile(r"omn15359_ac3_[a-z0-9_]{1,40}")
_PRODUCTION_MARKER_RE = re.compile(
    r"(?:^|[._:/@-])prod(?:uction)?(?:[._:/@-]|$)", re.IGNORECASE
)


class _RecordingPublisher:
    def __init__(self) -> None:
        self.count = 0

    def publish(self, *args: object, **kwargs: object) -> bool:
        self.count += 1
        return True


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixtures(path: Path) -> dict[str, set[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or len(raw) != 2:
        raise ReplayError("fixture JSON must map exactly two tenant UUIDs to IDs")
    expected: dict[str, set[str]] = {}
    seen: set[str] = set()
    for tenant, correlations in raw.items():
        if not isinstance(tenant, str) or str(UUID(tenant)) != tenant:
            raise ReplayError("fixture tenant must be a canonical UUID")
        if not isinstance(correlations, list) or not correlations:
            raise ReplayError("each fixture tenant needs non-empty correlation IDs")
        for correlation in correlations:
            if (
                not isinstance(correlation, str)
                or str(UUID(correlation)) != correlation
            ):
                raise ReplayError("fixture correlation must be a canonical UUID")
            if correlation in seen:
                raise ReplayError("fixture correlation belongs to multiple tenants")
            seen.add(correlation)
        expected[tenant] = set(correlations)
        if len(expected[tenant]) != len(correlations):
            raise ReplayError("fixture repeats a correlation ID")
    return expected


def _require_lane_identity(lane: str, environment: Mapping[str, str]) -> None:
    """Require two independent lane markers and refuse production endpoints."""
    expected_environment = _LANE_ENVIRONMENTS.get(lane)
    if expected_environment is None:
        raise ReplayError("unknown AC3 lane identity")
    if environment.get("AC3_LANE_IDENTITY", "").strip() != lane:
        raise ReplayError("AC3 lane identity is absent or ambiguous")
    if environment.get("ONEX_ENVIRONMENT", "").strip() != expected_environment:
        raise ReplayError(
            f"ONEX_ENVIRONMENT does not identify the {lane} environment"
        )
    guarded_values = (
        environment.get("KAFKA_BOOTSTRAP_SERVERS", ""),
        environment.get("AC3_SOURCE_DSN", ""),
        environment.get("AC3_REPLAY_DSN", ""),
        environment.get("AC3_REPLAY_ADMIN_DSN", ""),
    )
    if any(_PRODUCTION_MARKER_RE.search(value) for value in guarded_values):
        raise ReplayError("production endpoints are outside the approved AC3 scope")


def _fixture_events(
    expected: Mapping[str, set[str]],
) -> list[tuple[str, dict[str, object]]]:
    """Build the exact canonical envelopes for this bounded fixture."""
    topics = contract_replay_topics(CONTRACT)
    canonical = (DELEGATION_COMPLETED_TOPIC_V1, DELEGATION_FAILED_TOPIC_V1)
    if topics != canonical:
        raise ReplayError("owning contract changed the canonical terminal topics")
    pairs = sorted(
        (tenant, correlation)
        for tenant, correlations in expected.items()
        for correlation in correlations
    )
    if len(expected) != 2 or len(pairs) < 2:
        raise ReplayError("fixture requires two non-empty synthetic tenants")
    events: list[tuple[str, dict[str, object]]] = []
    for index, (tenant, correlation) in enumerate(pairs):
        topic = topics[index % len(topics)]
        status = "completed" if topic == DELEGATION_COMPLETED_TOPIC_V1 else "failed"
        payload: dict[str, object] = {
            "status": status,
            "correlation_id": correlation,
            "task_type": "omn15359-ac3-fixture",
            "tenant_id": tenant,
            "metrics": {"cost_usd": 0.0},
        }
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=payload,
            correlation_id=UUID(correlation),
            source_tool="omn15359-ac3-fixture",
            event_type=topic,
            tenant_id=tenant,
        )
        events.append((topic, envelope.model_dump(mode="json")))
    if {topic for topic, _envelope in events} != set(canonical):
        raise ReplayError("fixture does not exercise both canonical terminal topics")
    return events


def _registry_relation(conn: Any) -> tuple[str, str]:
    """Resolve one registry mirror from the source search path and known homes."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT n.nspname, c.relname, c.oid "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = 'tenant_registry_mirror' "
            "AND c.relkind IN ('r', 'p') "
            "AND (n.nspname = ANY(current_schemas(true)) "
            "OR n.nspname IN ('omninode_internal', 'public')) "
            "ORDER BY c.oid"
        )
        rows = cur.fetchall()
    distinct = {(str(schema), str(relation), int(oid)) for schema, relation, oid in rows}
    if not distinct:
        raise ReplayError("tenant registry mapping relation is missing")
    if len(distinct) != 1:
        raise ReplayError("tenant registry mapping relation is ambiguous")
    schema, relation, _oid = next(iter(distinct))
    return schema, relation


def _require_registry_mappings(
    source_dsn: str, expected: Mapping[str, set[str]]
) -> tuple[str, str]:
    """Require exactly one mirror row for each synthetic tenant."""
    if not source_dsn:
        raise ReplayError("AC3_SOURCE_DSN is required")
    from psycopg2 import sql  # type: ignore[import-untyped]

    source = PostgresSyncProjectionAdapter(source_dsn)
    conn = source._connect()  # noqa: SLF001 - sanctioned source identity read
    try:
        schema, relation = _registry_relation(conn)
        query = sql.SQL(
            "SELECT tenant_uuid::text, count(*) FROM {}.{} "
            "WHERE tenant_uuid = ANY(%s::uuid[]) GROUP BY tenant_uuid"
        ).format(sql.Identifier(schema), sql.Identifier(relation))
        with conn.cursor() as cur:
            cur.execute(query, (sorted(expected),))
            counts = {str(tenant): int(count) for tenant, count in cur.fetchall()}
        if set(counts) != set(expected) or any(count != 1 for count in counts.values()):
            raise ReplayError("tenant registry mapping is missing or duplicated")
        return schema, relation
    finally:
        conn.close()


def _write_receipt(path: Path, receipt: Mapping[str, object]) -> None:
    if path.exists():
        raise ReplayError("receipt output already exists")
    with path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


async def _publish_fixture(
    *,
    lane: str,
    expected: Mapping[str, set[str]],
    receipt_path: Path,
) -> dict[str, object]:
    """Publish only this run's canonical terminal fixture records."""
    _require_lane_identity(lane, os.environ)
    source_dsn = os.environ.get("AC3_SOURCE_DSN", "")
    bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "")
    if not bootstrap:
        raise ReplayError("KAFKA_BOOTSTRAP_SERVERS is required")
    if receipt_path.exists():
        raise ReplayError("receipt output already exists")
    _require_registry_mappings(source_dsn, expected)
    events = _fixture_events(expected)

    from omnibase_infra.event_bus.kafka_auth import (
        build_aiokafka_auth_kwargs_from_env,
    )

    physical_topics = dict(
        zip(
            (DELEGATION_COMPLETED_TOPIC_V1, DELEGATION_FAILED_TOPIC_V1),
            apply_topic_namespace_all(
                (DELEGATION_COMPLETED_TOPIC_V1, DELEGATION_FAILED_TOPIC_V1)
            ),
            strict=True,
        )
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap,
        value_serializer=lambda value: json.dumps(
            value, separators=(",", ":"), sort_keys=True
        ).encode("utf-8"),
        **build_aiokafka_auth_kwargs_from_env(),
    )
    published: list[dict[str, object]] = []
    await producer.start()
    try:
        for canonical_topic, envelope in events:
            payload = envelope["payload"]
            if not isinstance(payload, dict):
                raise ReplayError("fixture envelope payload is malformed")
            correlation = str(payload["correlation_id"])
            metadata = await producer.send_and_wait(
                physical_topics[canonical_topic],
                envelope,
                key=correlation.encode("utf-8"),
            )
            published.append(
                {
                    "tenant_id": envelope["tenant_id"],
                    "correlation_id": correlation,
                    "canonical_topic": canonical_topic,
                    "topic": metadata.topic,
                    "partition": metadata.partition,
                    "offset": metadata.offset,
                }
            )
    finally:
        await producer.stop()
    receipt: dict[str, object] = {
        "ticket": "OMN-15359",
        "criterion": "AC3-fixture",
        "result": "PUBLISHED",
        "lane": lane,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "published_records": len(published),
        "events": published,
    }
    _write_receipt(receipt_path, receipt)
    return receipt


def _source_projection_relation(conn: Any) -> tuple[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT n.nspname, c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.oid = to_regclass('delegation_events')"
        )
        row = cur.fetchone()
    if row is None:
        raise ReplayError("source delegation_events relation is missing")
    return str(row[0]), str(row[1])


def _database_marker(conn: Any) -> tuple[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(), pg_postmaster_start_time()::text")
        database, started = cur.fetchone()
    return str(database), str(started)


def _provision_target(
    *,
    lane: str,
    expected: Mapping[str, set[str]],
    replay_schema: str,
    receipt_path: Path,
) -> dict[str, object]:
    """Create one empty replay schema and copy only its registry mappings."""
    _require_lane_identity(lane, os.environ)
    if _REPLAY_SCHEMA_RE.fullmatch(replay_schema) is None:
        raise ReplayError("replay schema must match omn15359_ac3_<run>")
    if receipt_path.exists():
        raise ReplayError("receipt output already exists")
    source_dsn = os.environ.get("AC3_SOURCE_DSN", "")
    admin_dsn = os.environ.get("AC3_REPLAY_ADMIN_DSN", "")
    if not source_dsn or not admin_dsn:
        raise ReplayError("AC3_SOURCE_DSN and AC3_REPLAY_ADMIN_DSN are required")
    registry_schema, registry_relation = _require_registry_mappings(
        source_dsn, expected
    )

    from psycopg2 import sql

    source = PostgresSyncProjectionAdapter(source_dsn)
    admin = PostgresSyncProjectionAdapter(admin_dsn)
    source_conn = source._connect()  # noqa: SLF001 - exact source identity
    admin_conn = admin._connect()  # noqa: SLF001 - owned disposable-schema DDL
    try:
        source_schema, source_relation = _source_projection_relation(source_conn)
        if _database_marker(source_conn) != _database_marker(admin_conn):
            raise ReplayError("source and replay admin are not the same database")
        with admin_conn.cursor() as cur:
            cur.execute("SELECT to_regnamespace(%s)::oid", (replay_schema,))
            if cur.fetchone()[0] is not None:
                raise ReplayError("replay schema already exists")
            for schema, relation in (
                (source_schema, source_relation),
                (registry_schema, registry_relation),
            ):
                cur.execute("SELECT to_regclass(%s)::oid", (f"{schema}.{relation}",))
                if cur.fetchone()[0] is None:
                    raise ReplayError("source relations are not visible to replay admin")

        admin_conn.autocommit = False
        with admin_conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(replay_schema)))
            cur.execute(
                sql.SQL("CREATE TABLE {}.delegation_events "
                        "(LIKE {}.{} INCLUDING ALL)").format(
                    sql.Identifier(replay_schema),
                    sql.Identifier(source_schema),
                    sql.Identifier(source_relation),
                )
            )
            cur.execute(
                sql.SQL("CREATE TABLE {}.tenant_registry_mirror "
                        "(LIKE {}.{} INCLUDING ALL)").format(
                    sql.Identifier(replay_schema),
                    sql.Identifier(registry_schema),
                    sql.Identifier(registry_relation),
                )
            )
            cur.execute(
                sql.SQL("INSERT INTO {}.tenant_registry_mirror "
                        "SELECT * FROM {}.{} "
                        "WHERE tenant_uuid = ANY(%s::uuid[])").format(
                    sql.Identifier(replay_schema),
                    sql.Identifier(registry_schema),
                    sql.Identifier(registry_relation),
                ),
                (sorted(expected),),
            )
            registry_rows = cur.rowcount
            cur.execute(
                sql.SQL("SELECT count(*) FROM {}.delegation_events").format(
                    sql.Identifier(replay_schema)
                )
            )
            empty_rows = int(cur.fetchone()[0])
            if registry_rows != len(expected):
                raise ReplayError("disposable registry copy is incomplete")
            if empty_rows != 0:
                raise ReplayError("disposable replay target is not empty")
        admin_conn.commit()
    except BaseException:
        if not admin_conn.autocommit:
            admin_conn.rollback()
        raise
    finally:
        source_conn.close()
        admin_conn.close()
    receipt: dict[str, object] = {
        "ticket": "OMN-15359",
        "criterion": "AC3-target",
        "result": "PROVISIONED",
        "lane": lane,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "source_relation": f"{source_schema}.{source_relation}",
        "replay_schema": replay_schema,
        "replay_relation": f"{replay_schema}.delegation_events",
        "empty_target_rows": empty_rows,
        "registry_rows": registry_rows,
    }
    _write_receipt(receipt_path, receipt)
    return receipt


def _relation_identity(db: PostgresSyncProjectionAdapter) -> tuple[str, str, int, bool]:
    conn = db._connect()  # noqa: SLF001 - inspect the adapter's exact relation
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_database(), current_schema(), "
                "to_regclass('delegation_events')::oid, current_user, "
                "pg_postmaster_start_time(), "
                "row_security_active('delegation_events'::regclass)"
            )
            database, schema, oid, role, started, rls_active = cur.fetchone()
            if oid is None:
                raise ReplayError("delegation_events relation is absent")
            physical_key = f"{started.isoformat()}/{database}/{oid}"
            identity = f"{database}.{schema}.delegation_events/oid={oid}/role={role}/server_start={started.isoformat()}"
            cur.execute("SELECT COUNT(*) FROM delegation_events")
            count = cur.fetchone()[0]
            return physical_key, identity, count, bool(rls_active)
    finally:
        conn.close()


def _rows_for(
    db: PostgresSyncProjectionAdapter, expected: dict[str, set[str]]
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for tenant, correlations in expected.items():
        for correlation in correlations:
            found = db.query(
                "delegation_events",
                {"tenant_id": tenant, "correlation_id": correlation},
            )
            rows.extend(
                {
                    "tenant_id": str(row["tenant_id"]),
                    "correlation_id": str(row["correlation_id"]),
                }
                for row in found
            )
    return rows


async def _execute(
    *,
    lane: str,
    expected: dict[str, set[str]],
    writer_image: str,
    deployed_handler_sha256: str,
    receipt_path: Path,
    replay_schema: str,
) -> dict[str, object]:
    _require_lane_identity(lane, os.environ)
    if re.search(r"sha256:[0-9a-f]{64}(?:$|[^0-9a-f])", writer_image) is None:
        raise ReplayError("writer image lacks a measured sha256 digest")
    if _digest(HANDLER) != deployed_handler_sha256:
        raise ReplayError("local handler bytes differ from deployed writer handler")
    source_dsn = os.environ.get("AC3_SOURCE_DSN", "")
    replay_dsn = os.environ.get("AC3_REPLAY_DSN", "")
    bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "")
    if not source_dsn or not replay_dsn or not bootstrap:
        raise ReplayError(
            "AC3_SOURCE_DSN, AC3_REPLAY_DSN and KAFKA_BOOTSTRAP_SERVERS are required"
        )
    source = PostgresSyncProjectionAdapter(source_dsn)
    target = PostgresSyncProjectionAdapter(replay_dsn, schema=replay_schema)
    source_key, source_identity, _, _ = _relation_identity(source)
    target_key, target_identity, empty_rows, target_rls = _relation_identity(target)
    if source_key == target_key:
        raise ReplayError("source and replay resolve to the same relation")
    if target_rls:
        raise ReplayError("target RLS obscures global empty-copy verification")
    if empty_rows:
        raise ReplayError("replay copy must begin empty")

    from omnibase_infra.event_bus.kafka_auth import (
        build_aiokafka_auth_kwargs_from_env,
    )

    canonical_topics = contract_replay_topics(CONTRACT)
    physical_topics = tuple(apply_topic_namespace_all(canonical_topics))
    physical_to_canonical = dict(zip(physical_topics, canonical_topics, strict=True))
    factory = partial(
        AIOKafkaConsumer,
        bootstrap_servers=bootstrap,
        client_id="omn15359-ac3-read-only",
        value_deserializer=None,
        **build_aiokafka_auth_kwargs_from_env(),
    )
    records, spans = await capture_retained(factory, physical_topics)
    if {span.topic for span in spans} != set(physical_topics):
        raise ReplayError("broker offset vector omits a terminal topic")
    canonical_records = [
        replace(record, topic=physical_to_canonical[record.topic]) for record in records
    ]
    publisher = _RecordingPublisher()
    counts = replay_synthetic_records(
        canonical_records,
        expected,
        target,
        handler=HandlerProjectionDelegation(publisher=publisher),
    )
    comparison = compare_tenant_keys(
        expected,
        live_rows=_rows_for(source, expected),
        replay_rows=_rows_for(target, expected),
    )
    probe_hash = hashlib.sha256(
        b"".join(path.read_bytes() for path in (Path(__file__), MODULE, CONTRACT))
    ).hexdigest()
    receipt = build_receipt(
        lane=lane,
        writer_image=writer_image,
        handler_sha256=deployed_handler_sha256,
        probe_sha256=probe_hash,
        source_relation=source_identity,
        replay_relation=target_identity,
        empty_target_rows=empty_rows,
        source_topics=physical_topics,
        spans=spans,
        consumed_records=len(records),
        applied_records=counts,
        comparison=comparison,
        snapshots_suppressed=publisher.count,
    )
    _write_receipt(receipt_path, receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lane", required=True, choices=("operator-201", "shared-dev-201")
    )
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument(
        "--action",
        choices=("replay", "publish-fixture", "provision-target"),
        default="replay",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--writer-image")
    parser.add_argument("--deployed-handler-sha256")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--replay-schema")
    args = parser.parse_args(argv)
    try:
        expected = _fixtures(args.fixtures)
        topics = contract_replay_topics(CONTRACT)
        if args.dry_run:
            writes = {
                "publish-fixture": [
                    "canonical completed/failed fixture events for the listed IDs",
                    "--receipt immutable publish receipt",
                ],
                "provision-target": [
                    "new AC3_REPLAY_ADMIN_DSN schema named by --replay-schema",
                    "two tenant registry mappings; zero projection rows",
                    "--receipt immutable provisioning receipt",
                ],
                "replay": [
                    "AC3_REPLAY_DSN/--replay-schema empty delegation_events",
                    "--receipt immutable replay receipt",
                ],
            }[args.action]
            print(
                json.dumps(
                    {
                        "action": args.action,
                        "lane": args.lane,
                        "topics": apply_topic_namespace_all(topics),
                        "synthetic_tenants": {
                            tenant: sorted(ids) for tenant, ids in expected.items()
                        },
                        "reads": [
                            "contract broker topics from earliest retained offsets to fixed ends",
                            "AC3_SOURCE_DSN delegation_events",
                        ],
                        "writes": writes,
                        "publishes_events": args.action == "publish-fixture",
                        "publishes_snapshots": False,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        _require_lane_identity(args.lane, os.environ)
        if not args.receipt:
            raise ReplayError("--execute requires --receipt")
        if args.action == "publish-fixture":
            receipt = asyncio.run(
                _publish_fixture(
                    lane=args.lane,
                    expected=expected,
                    receipt_path=args.receipt,
                )
            )
            print(
                f"AC3 FIXTURE PUBLISHED receipt={args.receipt} "
                f"sha256={_digest(args.receipt)} records={receipt['published_records']}"
            )
            return 0
        if args.action == "provision-target":
            if not args.replay_schema:
                raise ReplayError("provision-target requires --replay-schema")
            receipt = _provision_target(
                lane=args.lane,
                expected=expected,
                replay_schema=args.replay_schema,
                receipt_path=args.receipt,
            )
            print(
                f"AC3 TARGET PROVISIONED receipt={args.receipt} "
                f"sha256={_digest(args.receipt)} schema={receipt['replay_schema']}"
            )
            return 0
        if not args.writer_image or not args.deployed_handler_sha256:
            raise ReplayError(
                "replay requires --writer-image and --deployed-handler-sha256"
            )
        if (
            not args.replay_schema
            or _REPLAY_SCHEMA_RE.fullmatch(args.replay_schema) is None
        ):
            raise ReplayError("replay requires --replay-schema=omn15359_ac3_<run>")
        asyncio.run(
            _execute(
                lane=args.lane,
                expected=expected,
                writer_image=args.writer_image,
                deployed_handler_sha256=args.deployed_handler_sha256,
                receipt_path=args.receipt,
                replay_schema=args.replay_schema,
            )
        )
        print(
            f"AC3 PASS receipt={args.receipt} sha256={_digest(args.receipt)} tenants={len(expected)}"
        )
        return 0
    except (ReplayError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"AC3 REFUSED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Driver exceptions can carry DSNs. Report the type only, never secrets.
        print(f"AC3 REFUSED: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
