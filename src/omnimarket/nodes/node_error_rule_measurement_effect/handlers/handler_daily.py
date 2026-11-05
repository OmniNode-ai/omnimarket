# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One scheduler tick measures every row from one frozen daily source snapshot."""

from omnimarket.models.error_rule_measurement import (
    ModelDailyMeasurementResult,
    ModelMeasurementCommand,
    ModelMeasurementOverlay,
    ModelMeasurementTick,
    ModelRuleMeasurementSnapshot,
)
from omnimarket.protocols.protocol_error_rule_measurement import (
    ProtocolMeasurementArtifacts,
    ProtocolMeasurementLabeller,
    ProtocolMeasurementReplay,
    ProtocolMeasurementSource,
    ProtocolMeasurementWriter,
)

from .handler_measurement import HandlerErrorRuleMeasurement


class HandlerErrorRuleMeasurementDaily:
    def __init__(
        self,
        *,
        overlay: ModelMeasurementOverlay,
        artifacts: ProtocolMeasurementArtifacts,
        replay: ProtocolMeasurementReplay,
        labeller: ProtocolMeasurementLabeller,
        writer: ProtocolMeasurementWriter,
        source: ProtocolMeasurementSource,
    ) -> None:
        self.source = source
        self.measurement = HandlerErrorRuleMeasurement(
            overlay=overlay,
            artifacts=artifacts,
            replay=replay,
            labeller=labeller,
            writer=writer,
        )

    def handle(self, request: ModelMeasurementTick) -> ModelDailyMeasurementResult:
        snapshot = self.source.snapshot(request.now)
        rows = [
            (
                ModelRuleMeasurementSnapshot.model_validate_json(
                    self.measurement.read_artifact(digest)
                ).id,
                digest,
            )
            for digest in snapshot.row_digests
        ]
        if len({row_id for row_id, _ in rows}) != len(rows):
            raise ValueError("daily source contains duplicate rule ids")
        results = tuple(
            self.measurement.handle(
                ModelMeasurementCommand(
                    now=request.now,
                    row_digest=digest,
                    corpus_digest=snapshot.corpus_digest,
                    normalizer_digest=snapshot.normalizer_digest,
                    live_digest=snapshot.live_digest,
                )
            )
            for _, digest in sorted(rows)
        )
        return ModelDailyMeasurementResult(
            measured_at=request.now, measurements=results
        )
