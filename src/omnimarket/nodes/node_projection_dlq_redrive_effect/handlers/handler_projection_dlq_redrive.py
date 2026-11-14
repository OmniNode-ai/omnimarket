# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT: re-project the records a projection writer refused (OMN-22811).

A refused record's dead-letter envelope names the topic, partition and offset
the record was read from. This handler reads each dead letter off the
dead-letter topic by correlation id, reads the source record again by those
coordinates (an assigned seek, never a consumer group, so no offset moves),
and projects it through the topic's projection writer.

A record whose tenant the registry still cannot resolve is skipped and left on
the dead-letter topic, so a later run picks it up once the tenant is
registered. A dead letter with no source coordinates (written before they were
recorded) is skipped with its own reason, never guessed at.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from omnimarket.models.model_message_meta import MessageMeta
from omnimarket.nodes.node_projection_dlq_redrive_effect.models import (
    EnumRedriveOutcome,
    EnumRedriveVerdict,
    ModelDeadLetterSource,
    ModelProjectionDlqRedriveRequest,
    ModelProjectionDlqRedriveResult,
    ModelRedriveRecordResult,
)
from omnimarket.nodes.node_projection_dlq_redrive_effect.protocols import (
    ProtocolDeadLetterReader,
    ProtocolRecordProjector,
    ProtocolSourceRecordReader,
    ProtocolTenantRegistry,
)
from omnimarket.projection.envelope import envelope_tenant_identity, unwrap_envelope
from omnimarket.projection.runner import deterministic_correlation_id

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_SKIPS = {
    EnumRedriveOutcome.SKIPPED_NO_SOURCE,
    EnumRedriveOutcome.SKIPPED_SOURCE_GONE,
    EnumRedriveOutcome.SKIPPED_TENANT_UNKNOWN,
    EnumRedriveOutcome.NOT_PROJECTED,
}


def contract_dlq_topic() -> str:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    topic: str = data["config"]["projection_dlq_redrive"]["dlq_topic"]
    return topic


class HandlerProjectionDlqRedrive:
    def __init__(
        self,
        *,
        dead_letters: ProtocolDeadLetterReader | None = None,
        source: ProtocolSourceRecordReader | None = None,
        tenants: ProtocolTenantRegistry | None = None,
        projector: ProtocolRecordProjector | None = None,
    ) -> None:
        if dead_letters is None or source is None:
            from omnimarket.nodes.node_projection_dlq_redrive_effect.handlers._kafka_readers import (
                LazyKafkaDeadLetterReader,
                LazyKafkaSourceReader,
            )

            dead_letters = dead_letters or LazyKafkaDeadLetterReader()
            source = source or LazyKafkaSourceReader()
        if tenants is None or projector is None:
            from omnimarket.nodes.node_projection_dlq_redrive_effect.handlers._delegation_boundary import (
                DelegationRedriveBoundary,
            )

            boundary = DelegationRedriveBoundary()
            tenants = tenants or boundary
            projector = projector or boundary
        self._dead_letters: ProtocolDeadLetterReader = dead_letters
        self._source: ProtocolSourceRecordReader = source
        self._tenants: ProtocolTenantRegistry = tenants
        self._projector: ProtocolRecordProjector = projector

    async def handle(
        self, request: ModelProjectionDlqRedriveRequest
    ) -> ModelProjectionDlqRedriveResult:
        dlq_topic = request.dlq_topic or contract_dlq_topic()
        raw_envelopes = await self._dead_letters.read_dead_letters(
            dlq_topic, frozenset(request.correlation_ids)
        )
        results: list[ModelRedriveRecordResult] = []
        seen: set[tuple[str, int, int]] = set()
        for raw in raw_envelopes:
            result = await self._redrive_one(raw, request.dry_run, seen)
            results.append(result)
        reprojected = sum(r.outcome is EnumRedriveOutcome.REPROJECTED for r in results)
        failed = sum(r.outcome is EnumRedriveOutcome.FAILED for r in results)
        skipped = sum(r.outcome in _SKIPS for r in results)
        return ModelProjectionDlqRedriveResult(
            dlq_topic=dlq_topic,
            dead_letters_read=len(raw_envelopes),
            reprojected=reprojected,
            skipped=skipped,
            failed=failed,
            verdict=_verdict(request.dry_run, results, skipped, failed),
            records=results,
        )

    async def _redrive_one(
        self, raw: bytes, dry_run: bool, seen: set[tuple[str, int, int]]
    ) -> ModelRedriveRecordResult:
        try:
            letter = ModelDeadLetterSource.model_validate(json.loads(raw))
        except ValueError as exc:
            return ModelRedriveRecordResult(
                correlation_id="",
                outcome=EnumRedriveOutcome.NOT_PROJECTED,
                detail=f"dead-letter envelope is not readable: {exc}",
            )
        cid = letter.correlation_id
        if (
            not letter.has_source()
            or letter.source_topic is None
            or letter.source_partition is None
            or letter.source_offset is None
        ):
            return ModelRedriveRecordResult(
                correlation_id=cid,
                outcome=EnumRedriveOutcome.SKIPPED_NO_SOURCE,
                detail="the envelope names no source topic, partition and offset",
            )
        topic, partition, offset = (
            letter.source_topic,
            letter.source_partition,
            letter.source_offset,
        )
        if (topic, partition, offset) in seen:
            return ModelRedriveRecordResult(
                correlation_id=cid,
                outcome=EnumRedriveOutcome.NOT_PROJECTED,
                detail="another dead letter already redrove this source record",
            )
        seen.add((topic, partition, offset))
        value = await self._source.read_record(topic, partition, offset)
        if value is None:
            return ModelRedriveRecordResult(
                correlation_id=cid,
                outcome=EnumRedriveOutcome.SKIPPED_SOURCE_GONE,
                detail=f"{topic}[{partition}]@{offset} is no longer on the broker",
            )
        data = unwrap_envelope(value)
        if data is None:
            return ModelRedriveRecordResult(
                correlation_id=cid,
                outcome=EnumRedriveOutcome.NOT_PROJECTED,
                detail="the source record is not a JSON message",
            )
        if not await self._tenants.tenant_is_registered(envelope_tenant_identity(data)):
            return ModelRedriveRecordResult(
                correlation_id=cid,
                outcome=EnumRedriveOutcome.SKIPPED_TENANT_UNKNOWN,
                detail="its tenant is still not registered; left on the dead-letter topic",
            )
        if dry_run:
            return ModelRedriveRecordResult(
                correlation_id=cid, outcome=EnumRedriveOutcome.DRY_RUN
            )
        meta = MessageMeta(
            partition=partition,
            offset=offset,
            fallback_id=deterministic_correlation_id(topic, partition, offset),
            topic=topic,
        )
        try:
            projected = await self._projector.project(topic, data, meta)
        except Exception as exc:
            return ModelRedriveRecordResult(
                correlation_id=cid,
                outcome=EnumRedriveOutcome.FAILED,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return ModelRedriveRecordResult(
            correlation_id=cid,
            outcome=(
                EnumRedriveOutcome.REPROJECTED
                if projected
                else EnumRedriveOutcome.NOT_PROJECTED
            ),
        )


def _verdict(
    dry_run: bool,
    results: list[ModelRedriveRecordResult],
    skipped: int,
    failed: int,
) -> EnumRedriveVerdict:
    if not results:
        return EnumRedriveVerdict.NOTHING_TO_REDRIVE
    if dry_run:
        return EnumRedriveVerdict.DRY_RUN
    if failed or skipped:
        return EnumRedriveVerdict.PARTIAL
    return EnumRedriveVerdict.REDRIVEN
