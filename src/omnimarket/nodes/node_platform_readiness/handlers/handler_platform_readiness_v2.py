# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodePlatformReadinessV2 — 7-dimension parallel orchestrator.

Replaces the pass-through dispatcher with a true orchestrator that:
1. Runs all 7 dimension checks in parallel via asyncio.gather
2. Aggregates to worst-dimension overall status (PASS < WARN < FAIL)
3. Writes YAML artifact to .onex_state/readiness/latest.yaml + timestamped snapshot
4. Emits Kafka event onex.evt.platform.readiness-assessed.v1
5. Returns ModelPlatformReadinessResult (existing type for backward compat)

Note (R2): Artifact path uses OMNI_HOME env var for absolute resolution.
Note (R8): Snapshot retention: keep last 30 files, delete older ones.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.nodes.node_platform_readiness.handlers.dimension_checks import (
    CheckContext,
    run_all_dimensions,
)
from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
    ModelDimensionResult,
    ModelPlatformReadinessResult,
)
from omnimarket.nodes.node_platform_readiness.models.dimension_result_v2 import (
    ModelDimensionResultV2,
)
from omnimarket.nodes.node_platform_readiness.topics import TOPIC_READINESS_ASSESSED

_OMNI_HOME = os.environ.get("OMNI_HOME", os.path.expanduser("~/Code/omni_home"))
_SNAPSHOT_RETENTION = 30


def _worst_status(statuses: list[EnumReadinessStatus]) -> EnumReadinessStatus:
    """Return worst status in order FAIL > WARN > PASS."""
    if EnumReadinessStatus.FAIL in statuses:
        return EnumReadinessStatus.FAIL
    if EnumReadinessStatus.WARN in statuses:
        return EnumReadinessStatus.WARN
    return EnumReadinessStatus.PASS


def _write_yaml_artifact(
    artifact_dir: Path,
    overall_status: EnumReadinessStatus,
    dimension_results: list[ModelDimensionResultV2],
    generated_at: datetime,
) -> Path:
    """Write latest.yaml and a timestamped snapshot. Returns path to latest.yaml."""
    artifact_dir.mkdir(parents=True, exist_ok=True)

    artifact = {
        "generated_at": generated_at.isoformat() + "Z",
        "overall_status": overall_status.value,
        # mode="json" serializes StrEnum values as plain strings (not enum objects)
        "dimensions": [d.model_dump(mode="json") for d in dimension_results],
    }
    yaml_content = yaml.dump(artifact, default_flow_style=False, sort_keys=False)

    latest_path = artifact_dir / "latest.yaml"
    latest_path.write_text(yaml_content)

    snapshot_name = f"snapshot-{generated_at.strftime('%Y%m%dT%H%M%S')}.yaml"
    (artifact_dir / snapshot_name).write_text(yaml_content)

    # Retention: delete oldest snapshots beyond the limit
    snapshots = sorted(artifact_dir.glob("snapshot-*.yaml"), key=lambda f: f.name)
    for old in snapshots[:-_SNAPSHOT_RETENTION]:
        old.unlink(missing_ok=True)

    return latest_path


def _emit_kafka_event(
    artifact: dict,
    topic: str,
) -> None:
    """Emit readiness event to Kafka. Best-effort — failure does not abort the run.

    Uses omnibase_infra KafkaPublisher if available. Falls back to no-op if Kafka
    is not reachable (dev/test environments where infra is not running).
    """
    with contextlib.suppress(Exception):
        import importlib

        publisher_mod = importlib.import_module("omnibase_infra.kafka.publisher")
        publisher = publisher_mod.KafkaPublisher()
        asyncio.get_event_loop().run_until_complete(
            publisher.publish(topic=topic, payload=json.dumps(artifact))
        )


class NodePlatformReadinessV2:
    """V2 orchestrator: runs 7 dimensions in parallel, writes YAML artifact."""

    def __init__(self, omni_home: str | None = None) -> None:
        self._omni_home = Path(omni_home or _OMNI_HOME)

    def handle_sync(
        self, ctx: CheckContext | None = None
    ) -> ModelPlatformReadinessResult:
        """Synchronous entry point — runs the async orchestrator in an event loop."""
        return asyncio.get_event_loop().run_until_complete(self.handle(ctx))

    async def handle(
        self, ctx: CheckContext | None = None
    ) -> ModelPlatformReadinessResult:
        """Run V2 orchestration: parallel checks → aggregate → write artifact → emit."""
        if ctx is None:
            ctx = CheckContext(omni_home=self._omni_home)

        generated_at = datetime.now(UTC)

        # 1. Run all 7 dimension checks in parallel
        dimension_results = await run_all_dimensions(ctx)

        # 2. Aggregate: worst-dimension wins
        statuses = [r.status for r in dimension_results]
        overall_status = _worst_status(statuses)

        # 3. Write YAML artifact
        artifact_dir = self._omni_home / ".onex_state" / "readiness"
        _write_yaml_artifact(
            artifact_dir, overall_status, dimension_results, generated_at
        )

        # 4. Emit Kafka event (best-effort)
        artifact_payload = {
            "generated_at": generated_at.isoformat() + "Z",
            "overall_status": overall_status.value,
            "dimensions": [d.model_dump(mode="json") for d in dimension_results],
        }
        with contextlib.suppress(Exception):
            _emit_kafka_event(artifact_payload, TOPIC_READINESS_ASSESSED)

        # 5. Return ModelPlatformReadinessResult (backward compat with V1 callers)
        blockers = [
            f"{r.dimension}: {r.actionable_items[0] if r.actionable_items else 'failed'}"
            for r in dimension_results
            if r.status == EnumReadinessStatus.FAIL
        ]
        degraded = [
            f"{r.dimension}: {r.actionable_items[0] if r.actionable_items else 'degraded'}"
            for r in dimension_results
            if r.status == EnumReadinessStatus.WARN
        ]

        # Translate V2 results to V1 ModelDimensionResult for backward compat
        v1_dimensions = [
            ModelDimensionResult(
                name=r.dimension,
                status=r.status,
                critical=r.status == EnumReadinessStatus.FAIL,
                freshness=(
                    f"{r.freshness_seconds // 3600}h ago"
                    if r.freshness_seconds and r.freshness_seconds > 0
                    else "current"
                ),
                details=(
                    r.actionable_items[0]
                    if r.actionable_items
                    else r.raw_detail or r.evidence_source
                ),
            )
            for r in dimension_results
        ]

        return ModelPlatformReadinessResult(
            overall=overall_status,
            dimensions=v1_dimensions,
            blockers=blockers,
            degraded=degraded,
            timestamp=generated_at,
        )
