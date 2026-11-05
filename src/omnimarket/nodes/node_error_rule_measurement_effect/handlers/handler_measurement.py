# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Daily replay and prospective evidence, committed and read back on the lab.

The bus composition injects content storage, the installed matcher/normalizer,
second-engine labelling and the PR write node. This effect grants no promotion.
All artifacts are addressed by exact bytes and re-read before use; a commit's
login comes from the writer's SCM read boundary, never the block's written_by.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import UTC, timedelta

from pydantic import JsonValue, TypeAdapter

from omnimarket.models.error_rule_measurement import (
    ModelDemotionProposal,
    ModelLabellingRequest,
    ModelLabelSet,
    ModelLiveMeasurementInput,
    ModelMeasuredErrorRule,
    ModelMeasurementCommand,
    ModelMeasurementOverlay,
    ModelMeasurementProposal,
    ModelMeasurementResult,
    ModelProspectiveMissRate,
    ModelReplayRecord,
    ModelRuleMeasurementSnapshot,
    ModelWilsonInterval,
)
from omnimarket.protocols.protocol_error_rule_measurement import (
    ProtocolMeasurementArtifacts,
    ProtocolMeasurementLabeller,
    ProtocolMeasurementReplay,
    ProtocolMeasurementWriter,
)


def artifact_digest(content: bytes) -> str:
    """Hash stored bytes, independent of a producer's claims about their content."""
    return hashlib.sha256(content).hexdigest()


def canonical_bytes(value: JsonValue) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


class HandlerErrorRuleMeasurement:
    """One daily row command; retries update the same measurement/demotion PR."""

    def __init__(
        self,
        *,
        overlay: ModelMeasurementOverlay,
        artifacts: ProtocolMeasurementArtifacts,
        replay: ProtocolMeasurementReplay,
        labeller: ProtocolMeasurementLabeller,
        writer: ProtocolMeasurementWriter,
    ) -> None:
        self.overlay = overlay
        self.artifacts = artifacts
        self.replay = replay
        self.labeller = labeller
        self.writer = writer

    def read_artifact(self, digest: str) -> bytes:
        content = self.artifacts.get(digest)
        if artifact_digest(content) != digest:
            raise ValueError("artifact digest differs from requested digest")
        return content

    def handle(self, request: ModelMeasurementCommand) -> ModelMeasurementResult:
        row = ModelRuleMeasurementSnapshot.model_validate_json(
            self.read_artifact(request.row_digest)
        )
        records = TypeAdapter(tuple[ModelReplayRecord, ...]).validate_json(
            self.read_artifact(request.corpus_digest)
        )
        normalizer = TypeAdapter(dict[str, JsonValue]).validate_json(
            self.read_artifact(request.normalizer_digest)
        )
        live = ModelLiveMeasurementInput.model_validate_json(
            self.read_artifact(request.live_digest)
        )
        if row.mode not in (
            self.overlay.shadow_mode,
            self.overlay.hint_mode,
            self.overlay.dispatch_mode,
        ):
            raise ValueError("unknown row mode")
        if self.overlay.labeller_engine == row.author_engine:
            raise ValueError("labeller must be a second engine")
        if any(
            key in row.definition for key in ("mode", "measured", "promotion_exception")
        ):
            raise ValueError("row definition must exclude lifecycle evidence")
        if row.definition.get("id") != row.id:
            raise ValueError("row definition id differs from snapshot")
        by_id = {record.record_id: record for record in records}
        if len(by_id) != len(records):
            raise ValueError("duplicate corpus record id")
        if any(record.occurred_at > request.now for record in records):
            raise ValueError("future corpus evidence cannot supply matches")
        matched = self.replay(row.definition, records, normalizer)
        if len(set(matched)) != len(matched) or not set(matched) <= by_id.keys():
            raise ValueError("replay returned duplicate or unknown corpus ids")
        # Sorting makes the simple random sample independent of matcher traversal.
        ids = sorted(matched)
        seed = artifact_digest(
            canonical_bytes(
                [request.row_digest, request.corpus_digest, request.normalizer_digest]
            )
        )
        chosen = sorted(
            random.Random(seed).sample(ids, min(len(ids), self.overlay.sample_limit))
        )
        effective_digest = artifact_digest(canonical_bytes(row.definition))
        labels = self.labeller(
            ModelLabellingRequest(
                row_digest=effective_digest,
                corpus_digest=request.corpus_digest,
                normalizer_digest=request.normalizer_digest,
                row=row,
                sample=tuple(by_id[key] for key in chosen),
                captures=live.captures if row.mode != self.overlay.shadow_mode else (),
                matches=live.matches if row.mode != self.overlay.shadow_mode else (),
                engine=self.overlay.labeller_engine,
            )
        )
        if (
            labels.engine != self.overlay.labeller_engine
            or labels.row_digest != effective_digest
            or labels.corpus_digest != request.corpus_digest
            or labels.normalizer_digest != request.normalizer_digest
        ):
            raise ValueError("label set provenance differs from replay")
        labelled_ids = [label.record_id for label in labels.matches]
        if len(set(labelled_ids)) != len(labelled_ids) or set(labelled_ids) != set(
            chosen
        ):
            raise ValueError("label set must cover exactly the sampled matches")
        capture_ids = [label.capture_id for label in labels.restatements]
        if row.mode != self.overlay.shadow_mode and (
            len(set(capture_ids)) != len(capture_ids)
            or set(capture_ids) != {capture.capture_id for capture in live.captures}
        ):
            raise ValueError("label set must cover every operator capture")
        label_content = canonical_bytes(labels.model_dump(mode="json"))
        labels_digest = self.artifacts.put(label_content)
        if (
            labels_digest != artifact_digest(label_content)
            or self.read_artifact(labels_digest) != label_content
        ):
            raise ValueError("label set read-back differs")
        true_records = [
            by_id[label.record_id] for label in labels.matches if label.applies
        ]
        disagreements = tuple(
            sorted(label.record_id for label in labels.matches if not label.applies)
        )
        n = len(labels.matches)
        false_count = len(disagreements)
        prospective, demote_ids = self._prospective(row, live, labels, request)
        measured = ModelMeasuredErrorRule(
            row_digest=effective_digest,
            row_snapshot_digest=request.row_digest,
            corpus_digest=request.corpus_digest,
            labels_digest=labels_digest,
            normalizer_digest=request.normalizer_digest,
            live_digest=request.live_digest,
            labeller_engine=labels.engine,
            labeller_run_id=labels.run_id,
            measured_at=request.now,
            written_by=self.overlay.write_identity,
            hits=len(ids),
            labelled_matches=n,
            sample_weight=len(ids) / n if n else 0,
            sample_ids=tuple(chosen),
            false_matches=false_count,
            true_matches=len(true_records),
            true_lanes=tuple(sorted({r.lane for r in true_records})),
            true_days=tuple(
                sorted(
                    {
                        r.occurred_at.astimezone(UTC).date().isoformat()
                        for r in true_records
                    }
                )
            ),
            oldest_true_match=min((r.occurred_at for r in true_records), default=None),
            false_match_rate=false_count / n if n else None,
            false_match_wilson=self._wilson(false_count, n) if n else None,
            disagreements=disagreements,
            prospective=prospective,
        )
        key = artifact_digest(
            canonical_bytes(
                [
                    row.id,
                    request.row_digest,
                    request.now.astimezone(UTC).date().isoformat(),
                ]
            )
        )
        commit_id = self.writer.write(
            ModelMeasurementProposal(
                row_id=row.id,
                idempotency_key=key,
                write_identity=self.overlay.write_identity,
                measured=measured,
            )
        )
        readback = self.writer.read(commit_id)
        if (
            readback is None
            or readback.commit_id != commit_id
            or readback.row_id != row.id
            or readback.measured != measured
        ):
            raise ValueError("measurement read-back differs or is absent")
        if (
            readback.login != self.overlay.write_identity
            or readback.login not in self.overlay.allowed_write_logins
        ):
            raise ValueError("measurement commit identity is not authorized")
        for digest in (
            request.row_digest,
            request.corpus_digest,
            request.normalizer_digest,
            request.live_digest,
            labels_digest,
        ):
            self.read_artifact(digest)
        demotion = None
        if demote_ids:
            demotion = self.writer.demote(
                ModelDemotionProposal(
                    row_id=row.id,
                    row_snapshot_digest=request.row_digest,
                    idempotency_key=artifact_digest(
                        canonical_bytes([row.id, request.row_digest, "demotion"])
                    ),
                    target_mode=self.overlay.shadow_mode,
                    capture_ids=demote_ids,
                    measured=measured,
                )
            )
        return ModelMeasurementResult(
            measured=measured, commit_id=commit_id, read_back=True, demotion_pr=demotion
        )

    def _wilson(self, count: int, denominator: int) -> ModelWilsonInterval:
        z = self.overlay.wilson_z
        proportion = count / denominator
        divisor = 1 + z * z / denominator
        center = (proportion + z * z / (2 * denominator)) / divisor
        radius = (
            z
            * math.sqrt(
                proportion * (1 - proportion) / denominator
                + z * z / (4 * denominator * denominator)
            )
            / divisor
        )
        return ModelWilsonInterval(
            lower=max(0, center - radius), upper=min(1, center + radius)
        )

    def _prospective(
        self,
        row: ModelRuleMeasurementSnapshot,
        live: ModelLiveMeasurementInput,
        labels: ModelLabelSet,
        request: ModelMeasurementCommand,
    ) -> tuple[ModelProspectiveMissRate | None, tuple[str, ...]]:
        matches = {match.match_id: match for match in live.matches}
        captures = {capture.capture_id: capture for capture in live.captures}
        if len(matches) != len(live.matches) or len(captures) != len(live.captures):
            raise ValueError("duplicate live MATCH or capture id")
        if row.mode == self.overlay.shadow_mode:
            if labels.restatements:
                raise ValueError("shadow cannot carry prospective restatements")
            return None, ()
        window = timedelta(days=self.overlay.restatement_window_days)
        confirmed: set[str] = set()
        pending: set[str] = set()
        demote: set[str] = set()
        seen: set[str] = set()
        confirmations = {c.capture_id: c for c in live.confirmations}
        if len(confirmations) != len(live.confirmations):
            raise ValueError("duplicate restatement confirmation")
        effective_digest = artifact_digest(canonical_bytes(row.definition))
        for label in labels.restatements:
            if label.capture_id in seen:
                raise ValueError("duplicate restatement label")
            seen.add(label.capture_id)
            if label.capture_id not in captures or (
                label.applies and label.match_id not in matches
            ):
                raise ValueError("restatement label names an unknown capture or MATCH")
            if not label.applies:
                continue
            if label.match_id is None:
                raise ValueError("restatement requires a MATCH id")
            capture, match = captures[label.capture_id], matches[label.match_id]
            if (
                match.row_id != row.id
                or match.row_digest != effective_digest
                or match.mode
                not in (self.overlay.hint_mode, self.overlay.dispatch_mode)
            ):
                raise ValueError(
                    "restatement label names another row or non-live MATCH"
                )
            # Future, pre-live and outside-window captures are never a measured miss.
            if (
                row.live_since is None
                or not row.live_since
                <= match.occurred_at
                <= capture.occurred_at
                <= request.now
                or capture.occurred_at > row.live_since + window
                or capture.occurred_at > match.occurred_at + window
            ):
                continue
            confirmation = confirmations.get(capture.capture_id)
            if confirmation is not None and (
                confirmation.match_id != match.match_id
                or confirmation.actor not in self.overlay.confirmation_actors
                or not capture.occurred_at <= confirmation.confirmed_at <= request.now
            ):
                raise ValueError("restatement confirmation provenance differs")
            if confirmation is None:
                pending.add(capture.capture_id)
                continue
            demote.add(capture.capture_id)
            if (
                row.hint_since is not None
                and match.mode == self.overlay.hint_mode
                and match.occurred_at >= row.hint_since
            ):
                confirmed.add(capture.capture_id)
        denominator = sum(
            1
            for match in matches.values()
            if match.row_id == row.id
            and match.row_digest == effective_digest
            and match.mode == self.overlay.hint_mode
            and row.hint_since is not None
            and row.hint_since <= match.occurred_at <= request.now
        )
        if row.hint_since is None or denominator == 0:
            return None, tuple(sorted(demote))
        return ModelProspectiveMissRate(
            hint_since=row.hint_since,
            observed_until=request.now,
            live_matches=denominator,
            restatements=len(confirmed),
            miss_rate=len(confirmed) / denominator,
            restatement_ids=tuple(sorted(confirmed)),
            pending_confirmation_ids=tuple(sorted(pending)),
        ), tuple(sorted(demote))
