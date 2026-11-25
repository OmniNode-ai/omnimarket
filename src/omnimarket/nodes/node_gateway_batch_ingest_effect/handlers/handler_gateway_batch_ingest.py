# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stateless batch ingest handler: bind the tenant, validate the whole batch, then publish and confirm each record.

Holds no store, claim or per-record container beyond one call: a retry of the same batch is admitted again and every
record is written again, byte-identical, which is the at-least-once delivery the consumers deduplicate by `message-id`.
"""

from __future__ import annotations

import logging

from omnimarket.nodes.node_gateway_batch_ingest_effect.models.model_batch_ingest import (
    ModelBatchIngestRequest,
    ModelBatchIngestResponse,
    ModelPublishOutcome,
    ModelWireRecord,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    ModelBatchIngestOverlay,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.protocols.protocol_record_publisher import (
    ProtocolRecordPublisher,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.protocols.protocol_tenant_bindings import (
    ProtocolTenantBindings,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.record_admission import (
    AdmissionRefusedError,
    admit_batch,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.topic_rules import slug_is_valid

logger = logging.getLogger(__name__)


def _refusal(
    status: int, reason: str, index: int | None = None, **extra: object
) -> ModelBatchIngestResponse:
    body: dict[str, object] = {"error": reason, **extra}
    if index is not None:
        body["index"] = index
    return ModelBatchIngestResponse(status_code=status, body=body)


class HandlerGatewayBatchIngest:
    """Handles one verified batch: `handle(request) -> response`."""

    def __init__(
        self,
        *,
        overlay: ModelBatchIngestOverlay,
        bindings: ProtocolTenantBindings,
        publisher: ProtocolRecordPublisher,
    ) -> None:
        self._overlay = overlay
        self._bindings = bindings
        self._publisher = publisher

    def _tenant_refusal(
        self, request: ModelBatchIngestRequest
    ) -> ModelBatchIngestResponse | None:
        claims = request.claims
        binding = self._bindings.binding_for(claims.tenant_id)
        if (
            binding is None
            or not slug_is_valid(claims.tenant_slug)
            or binding.tenant_slug != claims.tenant_slug
        ):
            return _refusal(403, "tenant-binding-mismatch")
        if binding.status == "retired":
            return _refusal(403, "tenant-retired")
        return None

    async def handle(
        self, request: ModelBatchIngestRequest
    ) -> ModelBatchIngestResponse:
        refused = self._tenant_refusal(request)
        if refused is not None:
            return refused
        if (
            self._publisher.message_max_bytes
            != self._overlay.kafka.producer_max_request_bytes
        ):
            return _refusal(500, "kafka-limit-drift", retryable=False)
        try:
            records = admit_batch(request.document, request.claims, self._overlay)
        except AdmissionRefusedError as exc:
            return _refusal(422, exc.reason, exc.index)
        return await self._publish(records)

    async def _publish(
        self, records: list[ModelWireRecord]
    ) -> ModelBatchIngestResponse:
        published = 0
        for index, record in enumerate(records):
            try:
                outcome = await self._publisher.publish(record)
            except Exception as exc:
                logger.warning("publisher raised %s", type(exc).__name__)
                outcome = ModelPublishOutcome(
                    status="publish-refused", detail=type(exc).__name__
                )
            if outcome.status != "published":
                return ModelBatchIngestResponse(
                    status_code=503,
                    body={
                        "error": "publish-failed",
                        "published": published,
                        "failed": {
                            "index": index,
                            "reason": outcome.status,
                            "retryable": True,
                        },
                        "not_attempted": len(records) - index - 1,
                    },
                )
            published += 1
        return ModelBatchIngestResponse(status_code=202, body={"published": published})


__all__ = ["HandlerGatewayBatchIngest"]
