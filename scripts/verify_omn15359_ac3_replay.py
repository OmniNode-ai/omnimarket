#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15359 AC3: bounded broker replay into a separately provisioned empty copy.

The fixture events must already have been published by an approved lane command.
This program reads the live projection and broker, writes only to AC3_REPLAY_DSN,
and never creates, truncates, or copies a table. Dry-run needs no credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from dataclasses import replace
from functools import partial
from pathlib import Path
from uuid import UUID

from aiokafka import AIOKafkaConsumer

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
) -> dict[str, object]:
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
    target = PostgresSyncProjectionAdapter(replay_dsn)
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
    with receipt_path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lane", required=True, choices=("operator-201", "shared-dev-201")
    )
    parser.add_argument("--fixtures", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--writer-image")
    parser.add_argument("--deployed-handler-sha256")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    try:
        expected = _fixtures(args.fixtures)
        topics = contract_replay_topics(CONTRACT)
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "lane": args.lane,
                        "topics": apply_topic_namespace_all(topics),
                        "synthetic_tenants": {
                            tenant: sorted(ids) for tenant, ids in expected.items()
                        },
                        "reads": [
                            "contract broker topics from earliest retained offsets to fixed ends",
                            "AC3_SOURCE_DSN delegation_events",
                        ],
                        "writes": [
                            "AC3_REPLAY_DSN empty delegation_events copy",
                            "--receipt output JSON",
                        ],
                        "publishes_events": False,
                        "publishes_snapshots": False,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if (
            not args.writer_image
            or not args.deployed_handler_sha256
            or not args.receipt
        ):
            raise ReplayError(
                "--execute requires --writer-image, --deployed-handler-sha256 and --receipt"
            )
        if args.receipt.exists():
            raise ReplayError("receipt output already exists")
        asyncio.run(
            _execute(
                lane=args.lane,
                expected=expected,
                writer_image=args.writer_image,
                deployed_handler_sha256=args.deployed_handler_sha256,
                receipt_path=args.receipt,
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
