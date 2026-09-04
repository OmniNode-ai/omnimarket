# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerCanaryOrchestrator — drives the ADR canary evaluation pipeline.

Pipeline per manifest entry:
  1. Ingest source documents via ProtocolAdrIngestion
  2. Segment each ingested document via ProtocolAdrSegmentation
  3. For each model: extract the immutable shared segments (concurrent, bounded)
  4. Grade each extraction via ProtocolAdrGrading
  5. Generate ADR draft via ProtocolAdrDraftGen
  6. Write evidence JSON to output_dir/<run_id>/<entry_id>/<model_key>.json
  7. Write scorecard.md to output_dir/<run_id>/scorecard.md

All protocols use shared ADR types from omnimarket.models.adr — no sibling
node package imports. Sub-node adapters translate shared → private models.
Topics are read from contract.yaml, never hardcoded.

[OMN-10698]
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import random
import string
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import yaml
from omnibase_core.models.adr.model_adr_draft import ModelADRDraft
from omnibase_core.models.adr.model_adr_extraction_metadata import (
    ModelADRExtractionMetadata,
)
from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.adr import (
    EnumAdrKBDestination,
    ModelAdrDocumentSegment,
    ModelAdrExtractionSummary,
    ModelAdrGradingScores,
    ModelAdrIngestionResult,
    ModelAdrManifestEntry,
    ModelAdrManifestModel,
    ModelAdrPublicationCandidate,
    ModelAdrSegmentationResult,
    ModelAdrSourceDocumentEvidence,
    ModelAdrSourceProvenance,
)
from omnimarket.nodes.node_adr_canary_orchestrator.models.model_canary_report import (
    ModelCanaryReport,
    ModelModelScore,
)
from omnimarket.nodes.node_adr_canary_orchestrator.models.model_canary_request import (
    ModelCanaryCommandPayload,
)

logger = logging.getLogger(__name__)

_ADR_CANARY_PIPELINE_VERSION = "adr-canary-orchestrator-v1"


# ---------------------------------------------------------------------------
# Protocol interfaces — defined by what the orchestrator needs, not by what
# the sub-nodes expose. Adapters (in tests or wired by runtime) translate.
# ---------------------------------------------------------------------------


@runtime_checkable
class ProtocolAdrIngestion(Protocol):
    """Consume root_paths; return document references."""

    async def ingest(
        self, *, root_paths: list[str], workspace_root: str
    ) -> ModelAdrIngestionResult: ...


@runtime_checkable
class ProtocolAdrExtraction(Protocol):
    """Extract decisions from shared semantic segments for a specific model."""

    async def extract(
        self,
        *,
        segments: tuple[ModelAdrDocumentSegment, ...],
        model_key: str,
        model_id: str,
        correlation_id: str,
    ) -> ModelAdrExtractionSummary: ...


@runtime_checkable
class ProtocolAdrSegmentation(Protocol):
    """Segment an ingestion result once before model-specific extraction."""

    async def segment(
        self,
        *,
        ingestion: ModelAdrIngestionResult,
        correlation_id: str,
    ) -> ModelAdrSegmentationResult: ...


@runtime_checkable
class ProtocolAdrGrading(Protocol):
    """Grade an extraction against a ground-truth ADR."""

    async def grade(
        self,
        *,
        ground_truth_adr: str,
        extraction: ModelAdrExtractionSummary,
        source_summary: str,
        correlation_id: str,
    ) -> ModelAdrGradingScores: ...


@runtime_checkable
class ProtocolAdrDraftGen(Protocol):
    """Generate an ADR draft markdown from an extraction summary."""

    async def generate(
        self,
        *,
        extraction: ModelAdrExtractionSummary,
        run_id: str,
    ) -> str: ...


# ---------------------------------------------------------------------------
# Evidence record
# ---------------------------------------------------------------------------


class ModelEvidenceRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    entry_id: str
    model_key: str
    model_id: str
    recall: float | None = None
    precision: float | None = None
    fidelity: float | None = None
    format_compliance: float | None = None
    extraction_success: bool = False
    grading_success: bool = False
    grading_not_applicable: bool = False
    draft_generated: bool = False
    extraction_error: str | None = None
    grading_error: str | None = None
    latency_ms: int = 0
    source_provenance: ModelAdrSourceProvenance | None = None
    kb_destination: EnumAdrKBDestination | None = None
    source_documents: tuple[ModelAdrSourceDocumentEvidence, ...] = ()


# ---------------------------------------------------------------------------
# Ground truth manifest (orchestrator-owned types)
# ---------------------------------------------------------------------------


class ModelGroundTruthManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: list[ModelAdrManifestEntry]
    # Optional top-level manifest metadata. The discovery manifest carries these
    # as real keys; the ground-truth manifest keeps them as comments. Declared
    # so both files load through the same model (OMN-14103).
    schema_version: str | None = None
    generated_at: str | None = None
    ticket: str | None = None
    source_provenance: ModelAdrSourceProvenance | None = None
    kb_destination: EnumAdrKBDestination | None = None

    @model_validator(mode="after")
    def _apply_explicit_manifest_publication_defaults(
        self,
    ) -> ModelGroundTruthManifest:
        """Carry manifest-owned publication facts to entries without inference.

        The defaults are authored directly in a manifest; no filesystem path,
        checkout name, or git remote participates in this decision. Per-entry
        facts remain authoritative when a mixed-source manifest needs them.
        """
        if self.source_provenance is None and self.kb_destination is None:
            return self
        if self.source_provenance is None or self.kb_destination is None:
            raise ValueError(
                "manifest source_provenance and kb_destination must be declared together"
            )
        entries = [
            entry.model_copy(
                update={
                    "source_provenance": entry.source_provenance
                    or self.source_provenance,
                    "kb_destination": entry.kb_destination or self.kb_destination,
                }
            )
            for entry in self.entries
        ]
        return self.model_copy(update={"entries": entries})


@dataclass(frozen=True, slots=True)
class _AdrProtocolAdapters:
    ingestion: ProtocolAdrIngestion
    segmentation: ProtocolAdrSegmentation
    extraction: ProtocolAdrExtraction
    grading: ProtocolAdrGrading
    draft_gen: ProtocolAdrDraftGen


@dataclass(frozen=True, slots=True)
class _RunModelOutcome:
    """Per-model result: the evidence record plus serialized ADR drafts.

    The ``decisions`` list carries fully serialized ``ModelADRDraft`` dicts
    (``model_dump(mode="json")``), each wrapped in a typed publication
    candidate with source provenance. The orchestrator aggregates these across
    all entries/models into the run-level ``extracted_decisions.json`` that
    ``HandlerKBADRPublisher`` consumes.
    """

    record: ModelEvidenceRecord
    decisions: list[dict[str, object]]


def _confidence_or_clamped(value: object) -> float:
    """Coerce a raw confidence value into the [0.0, 1.0] range ModelADRDraft requires."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        try:
            return max(0.0, min(1.0, float(value)))
        except ValueError:
            return 0.0
    return 0.0


def _decisions_from_extraction(
    extraction: ModelAdrExtractionSummary,
    model: ModelAdrManifestModel,
    entry: ModelAdrManifestEntry,
    source_documents: tuple[ModelAdrSourceDocumentEvidence, ...],
    run_id: str,
    now: datetime,
) -> list[dict[str, object]]:
    """Map each raw extraction dict into a serialized ``ModelADRDraft``.

    The publisher filters candidate drafts by ``extraction_metadata.model_id``;
    we set that to ``model.model_id`` so a ``model_key`` argument equal to the
    model id selects this model's decisions. Source provenance and hash-pinned,
    repository-relative documents travel with every candidate. Fields absent
    from the raw extraction (consequences, alternatives) are not fabricated.
    """
    decisions: list[dict[str, object]] = []
    for raw in extraction.extractions_raw:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("statement") or raw.get("title") or "Untitled decision")
        context_parts: list[str] = []
        rationale = raw.get("rationale")
        if rationale:
            context_parts.append(str(rationale))
        evidence_quotes = raw.get("evidence_quotes")
        if isinstance(evidence_quotes, list):
            context_parts.extend(str(q) for q in evidence_quotes)
        context = (
            "\n".join(context_parts)
            if context_parts
            else "(no rationale captured by the extraction pipeline)"
        )
        decision_type = str(raw.get("decision_type") or "").strip()
        decision = (
            title if not decision_type else f"{title} (decision_type: {decision_type})"
        )
        source_segment_ids = raw.get("source_segment_ids")
        source_evidence = (
            [str(seg) for seg in source_segment_ids]
            if isinstance(source_segment_ids, list)
            else []
        )
        draft = ModelADRDraft(
            date=now,
            title=title,
            context=context,
            decision=decision,
            consequences=(
                "Consequences were not captured by the extraction pipeline; "
                "complete during human review."
            ),
            alternatives_considered=[],
            supersedes=[],
            source_evidence=source_evidence,
            extraction_metadata=ModelADRExtractionMetadata(
                model_id=extraction.model_id or model.model_id,
                confidence=_confidence_or_clamped(raw.get("confidence")),
                pipeline_version=(
                    extraction.pipeline_version or _ADR_CANARY_PIPELINE_VERSION
                ),
                prompt_template_id=str(raw.get("prompt_template_id") or "unknown"),
                prompt_template_version=str(
                    raw.get("prompt_template_version") or "0.0.0"
                ),
                canary_run_id=run_id,
                extracted_at=now,
            ),
        )
        candidate = ModelAdrPublicationCandidate(
            draft=draft,
            source_provenance=entry.source_provenance,
            kb_destination=entry.kb_destination,
            source_documents=source_documents,
        )
        decisions.append(candidate.model_dump(mode="json"))
    return decisions


def _source_document_evidence(
    ingestion: ModelAdrIngestionResult,
) -> tuple[ModelAdrSourceDocumentEvidence, ...]:
    """Return hash-pinned source evidence, or an empty tuple that cannot publish.

    The canary may still retain extraction evidence from a legacy or malformed
    ingestion result, but that evidence must never become publishable without
    repository-relative paths and deterministic source hashes.
    """
    try:
        return tuple(
            ModelAdrSourceDocumentEvidence(
                source_path=document.source_path,
                source_content_sha256=document.source_content_sha256,
            )
            for document in ingestion.documents
        )
    except ValueError:
        logger.warning("ADR candidate source evidence is incomplete or non-canonical")
        return ()


# ---------------------------------------------------------------------------
# Scorecard writer
# ---------------------------------------------------------------------------


def _write_scorecard(
    run_id: str,
    manifest_path: str,
    scores: list[ModelModelScore],
    evidence_dir: Path,
    entries_total: int,
    entries_completed: int,
    entries_failed: int,
) -> Path:
    lines: list[str] = [
        f"# ADR Canary Scorecard — {run_id}",
        "",
        f"**Manifest:** `{manifest_path}`  ",
        f"**Entries:** {entries_total} total / {entries_completed} completed / {entries_failed} failed",
        "",
        "## Model Rankings",
        "",
        "| Model | Extraction | Grading | Recall | Precision | Fidelity | Format | Latency (ms) |",
        "|-------|------------|---------|--------|-----------|----------|--------|--------------|",
    ]

    def _fmt(v: float | None) -> str:
        return f"{v:.3f}" if v is not None else "—"

    def _grading_status(score: ModelModelScore) -> str:
        statuses: list[str] = []
        if score.entries_evaluated:
            statuses.append(f"{score.entries_evaluated} evaluated")
        if score.entries_grading_not_applicable:
            statuses.append("N/A")
        if score.entries_failed:
            statuses.append(f"{score.entries_failed} failed")
        return " / ".join(statuses) or "not run"

    for score in sorted(
        scores,
        key=lambda s: (s.avg_recall or 0.0) + (s.avg_precision or 0.0),
        reverse=True,
    ):
        lines.append(
            f"| {score.model_key} | {score.entries_extracted} succeeded "
            f"| {_grading_status(score)} | {_fmt(score.avg_recall)} | {_fmt(score.avg_precision)} "
            f"| {_fmt(score.avg_fidelity)} | {_fmt(score.avg_format_compliance)} "
            f"| {score.total_latency_ms} |"
        )

    lines += ["", f"*Generated: {datetime.now(UTC).isoformat()}*", ""]
    scorecard_path = evidence_dir / "scorecard.md"
    scorecard_path.write_text("\n".join(lines), encoding="utf-8")
    return scorecard_path


# ---------------------------------------------------------------------------
# Orchestrator handler
# ---------------------------------------------------------------------------


class HandlerCanaryOrchestrator:
    """ORCHESTRATOR handler driving the ADR canary evaluation pipeline.

    All sub-capabilities are injected via protocol interfaces. The handler
    never imports from sibling node packages — it only depends on shared
    types in omnimarket.models.adr and its own models.

    Config is read from contract.yaml via from_contract(). Topics are loaded
    from the contract at construction time — never hardcoded here.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effectful"] = "effectful"

    def __init__(self, container: object) -> None:
        adapters = _resolve_adr_protocol_adapters(container)
        self._container = container
        self._ingestion = adapters.ingestion
        self._segmentation = adapters.segmentation
        self._extraction = adapters.extraction
        self._grading = adapters.grading
        self._draft_gen = adapters.draft_gen
        self._max_concurrent_extractions = 4
        self._grader_model_key = "opus"
        self._allow_external_providers = False
        self._topic_completed = ""

    # ------------------------------------------------------------------
    # Manifest loading — path must be absolute or resolvable
    # ------------------------------------------------------------------

    def _load_manifest(self, manifest_path: str) -> ModelGroundTruthManifest:
        path = Path(manifest_path)
        with path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        return ModelGroundTruthManifest.model_validate(raw)

    # ------------------------------------------------------------------
    # Per-model pipeline
    # ------------------------------------------------------------------

    async def _run_model(
        self,
        entry: ModelAdrManifestEntry,
        model: ModelAdrManifestModel,
        run_id: str,
        ingestion: ModelAdrIngestionResult,
        segments: tuple[ModelAdrDocumentSegment, ...],
        evidence_entry_dir: Path,
        allow_external_providers: bool,
    ) -> _RunModelOutcome:
        source_documents = _source_document_evidence(ingestion)
        if model.external and not allow_external_providers:
            logger.info(
                "Skipping external model %s (allow_external_providers=False)", model.key
            )
            return _RunModelOutcome(
                record=ModelEvidenceRecord(
                    run_id=run_id,
                    entry_id=entry.id,
                    model_key=model.key,
                    model_id=model.model_id,
                    grading_not_applicable=entry.ground_truth_adr is None,
                    extraction_error="external_provider_disabled",
                    source_provenance=entry.source_provenance,
                    kb_destination=entry.kb_destination,
                    source_documents=source_documents,
                ),
                decisions=[],
            )

        t0 = time.monotonic()
        record = ModelEvidenceRecord(
            run_id=run_id,
            entry_id=entry.id,
            model_key=model.key,
            model_id=model.model_id,
            grading_not_applicable=entry.ground_truth_adr is None,
            source_provenance=entry.source_provenance,
            kb_destination=entry.kb_destination,
            source_documents=source_documents,
        )
        extraction_proto = self._extraction
        grading_proto = self._grading
        draft_gen_proto = self._draft_gen

        # Step 1: extract
        try:
            extraction = await extraction_proto.extract(
                segments=segments,
                model_key=model.key,
                model_id=model.model_id,
                correlation_id=run_id,
            )
        except Exception as exc:
            logger.warning(
                "Extraction failed (entry=%s, model=%s): %s", entry.id, model.key, exc
            )
            record = record.model_copy(
                update={
                    "extraction_error": f"{type(exc).__name__}: {exc}",
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                }
            )
            _write_evidence(evidence_entry_dir, model.key, record)
            return _RunModelOutcome(record=record, decisions=[])

        if not extraction.success:
            record = record.model_copy(
                update={
                    "extraction_error": extraction.error_message or "extraction failed",
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                }
            )
            _write_evidence(evidence_entry_dir, model.key, record)
            return _RunModelOutcome(record=record, decisions=[])

        extraction = extraction.model_copy(
            update={
                "model_id": model.model_id,
                "pipeline_version": _ADR_CANARY_PIPELINE_VERSION,
            }
        )
        record = record.model_copy(update={"extraction_success": True})

        # Structured ADR drafts for the run-level extracted_decisions.json (the
        # publisher input). Derived purely from the extraction — independent of
        # grading — so discovery entries (no ground truth) still produce drafts.
        decisions = _decisions_from_extraction(
            extraction,
            model,
            entry,
            source_documents,
            run_id,
            datetime.now(UTC),
        )

        # Step 2: grade — skipped for discovery entries that have no ground truth.
        if entry.ground_truth_adr is not None:
            source_summary = "\n".join(d.source_path for d in ingestion.documents)
            try:
                scores = await grading_proto.grade(
                    ground_truth_adr=entry.ground_truth_adr,
                    extraction=extraction,
                    source_summary=source_summary,
                    correlation_id=run_id,
                )
            except Exception as exc:
                logger.warning(
                    "Grading failed (entry=%s, model=%s): %s",
                    entry.id,
                    model.key,
                    exc,
                )
                record = record.model_copy(
                    update={
                        "grading_error": f"{type(exc).__name__}: {exc}",
                        "latency_ms": int((time.monotonic() - t0) * 1000),
                    }
                )
                _write_evidence(evidence_entry_dir, model.key, record)
                return _RunModelOutcome(record=record, decisions=decisions)

            if scores.success:
                record = record.model_copy(
                    update={
                        "grading_success": True,
                        "recall": scores.recall,
                        "precision": scores.precision,
                        "fidelity": scores.fidelity,
                        "format_compliance": scores.format_compliance,
                    }
                )

        # Step 3: generate ADR draft
        if extraction.extraction_count > 0:
            try:
                draft_md = await draft_gen_proto.generate(
                    extraction=extraction,
                    run_id=run_id,
                )
                if draft_md:
                    draft_path = evidence_entry_dir / f"{model.key}_draft.md"
                    draft_path.write_text(draft_md, encoding="utf-8")
                    record = record.model_copy(update={"draft_generated": True})
            except Exception as exc:
                logger.warning(
                    "Draft generation failed (entry=%s, model=%s): %s",
                    entry.id,
                    model.key,
                    exc,
                )

        record = record.model_copy(
            update={"latency_ms": int((time.monotonic() - t0) * 1000)}
        )
        _write_evidence(evidence_entry_dir, model.key, record)
        return _RunModelOutcome(record=record, decisions=decisions)

    # ------------------------------------------------------------------
    # Main entrypoint
    # ------------------------------------------------------------------

    async def handle(self, request: ModelCanaryCommandPayload) -> ModelCanaryReport:
        run_id = request.resume_run_id or _make_run_id()
        logger.info(
            "adr-canary started (run_id=%s, dry_run=%s)", run_id, request.dry_run
        )

        if request.dry_run:
            try:
                manifest = self._load_manifest(request.manifest_path)
            except Exception as exc:
                return ModelCanaryReport(
                    run_id=run_id,
                    manifest_path=request.manifest_path,
                    evidence_dir=str(Path(request.output_dir) / run_id),
                    scorecard_path=str(
                        Path(request.output_dir) / run_id / "scorecard.md"
                    ),
                    dry_run=True,
                    success=False,
                    error_message=f"manifest load failed: {exc}",
                )
            return ModelCanaryReport(
                run_id=run_id,
                manifest_path=request.manifest_path,
                entries_total=len(manifest.entries),
                evidence_dir=str(Path(request.output_dir) / run_id),
                scorecard_path=str(Path(request.output_dir) / run_id / "scorecard.md"),
                dry_run=True,
            )

        try:
            manifest = self._load_manifest(request.manifest_path)
        except Exception as exc:
            logger.error("Failed to load manifest: %s", exc)
            return ModelCanaryReport(
                run_id=run_id,
                manifest_path=request.manifest_path,
                evidence_dir=str(Path(request.output_dir) / run_id),
                scorecard_path=str(Path(request.output_dir) / run_id / "scorecard.md"),
                success=False,
                error_message=f"manifest load failed: {exc}",
            )

        # The extraction protocol currently reports post-call evidence only.
        # A hard budget needs a prospective, contract-owned upper bound before
        # the first LLM call; accepting a cap and discovering an overage later
        # would not enforce it. Fail closed until that accounting contract exists.
        if request.max_cost_usd is not None:
            return ModelCanaryReport(
                run_id=run_id,
                manifest_path=request.manifest_path,
                evidence_dir=str(Path(request.output_dir) / run_id),
                scorecard_path=str(Path(request.output_dir) / run_id / "scorecard.md"),
                success=False,
                error_message=(
                    "max_cost_usd is fail-closed: ADR extraction lacks a "
                    "prospective metering bound backed by cost_pricing"
                ),
            )

        try:
            workspace_root = _resolve_workspace_root(request.workspace_root)
        except ValueError as exc:
            return ModelCanaryReport(
                run_id=run_id,
                manifest_path=request.manifest_path,
                evidence_dir=str(Path(request.output_dir) / run_id),
                scorecard_path=str(Path(request.output_dir) / run_id / "scorecard.md"),
                success=False,
                error_message=str(exc),
            )

        evidence_dir = Path(request.output_dir) / run_id
        evidence_dir.mkdir(parents=True, exist_ok=True)

        try:
            entries = _select_manifest_entries(
                manifest.entries,
                request=request,
                workspace_root=workspace_root,
            )
        except ValueError as exc:
            return ModelCanaryReport(
                run_id=run_id,
                manifest_path=request.manifest_path,
                evidence_dir=str(evidence_dir),
                scorecard_path=str(evidence_dir / "scorecard.md"),
                success=False,
                error_message=str(exc),
            )

        ingestion_proto = self._ingestion
        segmentation_proto = self._segmentation
        sem = asyncio.Semaphore(self._max_concurrent_extractions)

        all_records: list[ModelEvidenceRecord] = []
        all_decisions: list[dict[str, object]] = []
        entries_completed = 0
        entries_failed = 0
        allow_external_providers = (
            self._allow_external_providers and request.allow_external_providers
        )

        for entry in entries:
            evidence_entry_dir = evidence_dir / entry.id
            evidence_entry_dir.mkdir(parents=True, exist_ok=True)

            # Ingest documents for this entry
            try:
                ingestion_result = await ingestion_proto.ingest(
                    root_paths=entry.root_paths,
                    workspace_root=workspace_root,
                )
            except Exception as exc:
                logger.error("Ingestion failed for entry %s: %s", entry.id, exc)
                entries_failed += 1
                continue

            # Filter models by subset
            models_to_run = entry.models
            if request.model_subset:
                models_to_run = [
                    m for m in entry.models if m.key in request.model_subset
                ]

            needs_segmentation = any(
                not model.external or allow_external_providers
                for model in models_to_run
            )
            if needs_segmentation:
                try:
                    segmentation_result = await segmentation_proto.segment(
                        ingestion=ingestion_result,
                        correlation_id=run_id,
                    )
                except Exception as exc:
                    segmentation_result = ModelAdrSegmentationResult(
                        success=False,
                        error_code="SEGMENTATION_PROTOCOL_FAILED",
                        error_message=f"{type(exc).__name__}: {exc}",
                    )

                if not segmentation_result.success:
                    error_message = (
                        segmentation_result.error_message or "segmentation failed"
                    )
                    for model in models_to_run:
                        model_error = (
                            "external_provider_disabled"
                            if model.external and not allow_external_providers
                            else error_message
                        )
                        record = ModelEvidenceRecord(
                            run_id=run_id,
                            entry_id=entry.id,
                            model_key=model.key,
                            model_id=model.model_id,
                            grading_not_applicable=entry.ground_truth_adr is None,
                            extraction_error=model_error,
                        )
                        all_records.append(record)
                        _write_evidence(evidence_entry_dir, model.key, record)
                    entries_failed += 1
                    continue
                segments = segmentation_result.segments
            else:
                # Do not invoke the local segmentation model for an entry whose
                # only selected models are policy-blocked external providers.
                segments = ()

            # Run models concurrently (bounded by semaphore)
            async def _bounded(
                _entry: ModelAdrManifestEntry = entry,
                _model: ModelAdrManifestModel = None,  # type: ignore[assignment]
                _ing: ModelAdrIngestionResult = ingestion_result,
                _segments: tuple[ModelAdrDocumentSegment, ...] = segments,
                _edir: Path = evidence_entry_dir,
            ) -> _RunModelOutcome:
                async with sem:
                    return await self._run_model(
                        _entry,
                        _model,
                        run_id,
                        _ing,
                        _segments,
                        _edir,
                        allow_external_providers,
                    )

            tasks = [asyncio.create_task(_bounded(_model=m)) for m in models_to_run]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            entry_ok = True
            for outcome in results:
                if isinstance(outcome, BaseException):
                    logger.error("Model task raised: %s", outcome)
                    entry_ok = False
                else:
                    all_records.append(outcome.record)
                    all_decisions.extend(outcome.decisions)
                    if outcome.record.extraction_error is not None:
                        entry_ok = False

            if entry_ok:
                entries_completed += 1
            else:
                entries_failed += 1

        # Run-level publisher input — the flat list of ModelADRDraft dicts the
        # kb_adr_publisher reads and filters by extraction_metadata.model_id.
        decisions_path = evidence_dir / "extracted_decisions.json"
        decisions_path.write_text(
            json.dumps(all_decisions, indent=2, sort_keys=True), encoding="utf-8"
        )
        logger.info(
            "Wrote %d extracted decisions to %s", len(all_decisions), decisions_path
        )

        model_scores = _aggregate_scores(all_records)
        scorecard_path = _write_scorecard(
            run_id=run_id,
            manifest_path=request.manifest_path,
            scores=model_scores,
            evidence_dir=evidence_dir,
            entries_total=len(entries),
            entries_completed=entries_completed,
            entries_failed=entries_failed,
        )

        logger.info(
            "adr-canary complete (run_id=%s, entries=%d/%d)",
            run_id,
            entries_completed,
            len(entries),
        )

        return ModelCanaryReport(
            run_id=run_id,
            manifest_path=request.manifest_path,
            entries_total=len(entries),
            entries_completed=entries_completed,
            entries_failed=entries_failed,
            model_scores=model_scores,
            evidence_dir=str(evidence_dir),
            scorecard_path=str(scorecard_path),
            success=entries_failed == 0,
            error_message=(
                None
                if entries_failed == 0
                else f"{entries_failed} manifest entr{'y' if entries_failed == 1 else 'ies'} failed"
            ),
        )

    @classmethod
    def from_contract(
        cls,
        contract: dict[str, Any],
        *,
        container: object,
    ) -> HandlerCanaryOrchestrator:
        """Build from a loaded contract.yaml dict with container-owned dependencies."""
        cfg = contract.get("config", {})
        event_bus = contract.get("event_bus", {})
        publish_topics: list[str] = event_bus.get("publish_topics", [])
        topic_completed = next((t for t in publish_topics if "completed" in t), "")
        handler = cls(container)
        handler._max_concurrent_extractions = int(
            cfg.get("max_concurrent_extractions", {}).get("default", 4)
        )
        handler._grader_model_key = str(
            cfg.get("grader_model_key", {}).get("default", "opus")
        )
        handler._allow_external_providers = bool(
            cfg.get("allow_external_providers", {}).get("default", False)
        )
        handler._topic_completed = topic_completed
        return handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_adr_protocol_adapters(container: object) -> _AdrProtocolAdapters:
    ingestion = _resolve_container_dependency(
        container, ProtocolAdrIngestion, "ingestion"
    )
    segmentation = _resolve_container_dependency(
        container, ProtocolAdrSegmentation, "segmentation"
    )
    extraction = _resolve_container_dependency(
        container, ProtocolAdrExtraction, "extraction"
    )
    grading = _resolve_container_dependency(container, ProtocolAdrGrading, "grading")
    draft_gen = _resolve_container_dependency(
        container, ProtocolAdrDraftGen, "draft_gen"
    )

    if (
        ingestion is None
        or segmentation is None
        or extraction is None
        or grading is None
        or draft_gen is None
    ):
        from omnimarket.adapters.adr import build_adr_bus_protocol_adapters

        bus_adapters = build_adr_bus_protocol_adapters(container)
        ingestion = ingestion or bus_adapters.ingestion
        segmentation = segmentation or bus_adapters.segmentation
        extraction = extraction or bus_adapters.extraction
        grading = grading or bus_adapters.grading
        draft_gen = draft_gen or bus_adapters.draft_gen

    return _AdrProtocolAdapters(
        ingestion=_require_protocol("ingestion", ingestion, ProtocolAdrIngestion),
        segmentation=_require_protocol(
            "segmentation", segmentation, ProtocolAdrSegmentation
        ),
        extraction=_require_protocol("extraction", extraction, ProtocolAdrExtraction),
        grading=_require_protocol("grading", grading, ProtocolAdrGrading),
        draft_gen=_require_protocol("draft_gen", draft_gen, ProtocolAdrDraftGen),
    )


def _resolve_container_dependency(
    container: object,
    protocol_type: object,
    service_name: str,
) -> Any | None:
    if isinstance(container, dict):
        value = container.get(service_name) or container.get(protocol_type)
        return value if value is not None else None

    for method_name in (
        "get_service",
        "get_service_sync",
        "get_service_optional",
    ):
        method = getattr(container, method_name, None)
        if not callable(method):
            continue
        for args, kwargs in (
            ((protocol_type,), {"service_name": service_name}),
            ((protocol_type,), {}),
            ((service_name,), {}),
        ):
            resolved = _call_optional(method, *args, **kwargs)
            if resolved is not None:
                return resolved

    for method_name in ("resolve", "get"):
        method = getattr(container, method_name, None)
        if not callable(method):
            continue
        for key in (service_name, protocol_type):
            resolved = _call_optional(method, key)
            if resolved is not None:
                return resolved

    if hasattr(container, service_name):
        return getattr(container, service_name)
    return None


def _call_optional(
    method: Callable[..., object],
    *args: object,
    **kwargs: object,
) -> object | None:
    try:
        return method(*args, **kwargs)
    except (AttributeError, KeyError, LookupError, RuntimeError, TypeError, ValueError):
        return None


def _require_protocol(
    service_name: str,
    candidate: object | None,
    protocol_type: object,
) -> Any:
    method_name = {
        "ingestion": "ingest",
        "segmentation": "segment",
        "extraction": "extract",
        "grading": "grade",
        "draft_gen": "generate",
    }[service_name]
    if candidate is None or not callable(getattr(candidate, method_name, None)):
        protocol_name = getattr(protocol_type, "__name__", str(protocol_type))
        raise TypeError(
            "HandlerCanaryOrchestrator container did not resolve "
            f"{service_name!r} as {protocol_name}."
        )
    return candidate


def _make_run_id() -> str:
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{ts}-{suffix}"


def _resolve_workspace_root(request_workspace_root: str | None) -> str:
    """Return a canonical workspace root for discovery-manifest source paths."""
    raw_workspace_root = request_workspace_root or os.environ.get("OMNI_HOME")
    if not raw_workspace_root:
        raise ValueError(
            "workspace_root or OMNI_HOME is required to resolve discovery source paths"
        )
    workspace_root = Path(raw_workspace_root).expanduser().resolve()
    if not workspace_root.is_dir():
        raise ValueError(f"workspace_root is not a directory: {workspace_root}")
    return str(workspace_root)


def _select_manifest_entries(
    entries: list[ModelAdrManifestEntry],
    *,
    request: ModelCanaryCommandPayload,
    workspace_root: str,
) -> list[ModelAdrManifestEntry]:
    """Return manifest entries authorized by an optional typed source scope.

    ``scoped_files`` are already syntax-validated by the command model. This
    boundary resolves them below the declared workspace and selects only entries
    whose authored roots cover those files. It never derives source visibility,
    repository identity, or destination from paths.
    """
    if request.source_provenance is None and request.kb_destination is not None:
        raise ValueError("kb_destination requires explicit source_provenance")
    if request.source_provenance is not None and request.kb_destination is None:
        raise ValueError("source_provenance requires explicit kb_destination")

    workspace = Path(workspace_root).resolve()
    scoped_files = request.scoped_files
    resolved_scope: tuple[tuple[str, Path], ...] = ()
    if scoped_files is not None:
        candidates: list[tuple[str, Path]] = []
        for source_path in scoped_files:
            candidate = (workspace / source_path).resolve()
            try:
                candidate.relative_to(workspace)
            except ValueError as exc:
                raise ValueError(
                    f"scoped file escapes workspace_root: {source_path}"
                ) from exc
            if not candidate.is_file():
                raise ValueError(f"scoped file is not an existing file: {source_path}")
            candidates.append((source_path, candidate))
        resolved_scope = tuple(candidates)

    selected: list[ModelAdrManifestEntry] = []
    for entry in entries:
        if request.source_provenance is not None:
            if entry.source_provenance != request.source_provenance:
                raise ValueError(
                    f"manifest entry {entry.id!r} source_provenance conflicts with command"
                )
            if entry.kb_destination != request.kb_destination:
                raise ValueError(
                    f"manifest entry {entry.id!r} kb_destination conflicts with command"
                )
        if not resolved_scope:
            selected.append(entry)
            continue

        matched_paths = [
            source_path
            for source_path, _ in resolved_scope
            if _entry_root_covers_source_path(entry.root_paths, source_path)
        ]
        if matched_paths:
            selected.append(entry.model_copy(update={"root_paths": matched_paths}))

    if scoped_files is not None and not selected:
        raise ValueError("no manifest entry covers the requested scoped_files")
    return selected


def _entry_root_covers_source_path(root_paths: list[str], source_path: str) -> bool:
    """Return whether an authored manifest root covers a workspace-relative file."""
    for raw_root in root_paths:
        normalized_root = raw_root.rstrip("/")
        if any(token in normalized_root for token in ("*", "?", "[")):
            if fnmatch.fnmatchcase(source_path, normalized_root):
                return True
            continue
        if source_path == normalized_root or source_path.startswith(
            normalized_root + "/"
        ):
            return True
    return False


def _write_evidence(
    evidence_entry_dir: Path, model_key: str, record: ModelEvidenceRecord
) -> None:
    out = evidence_entry_dir / f"{model_key}.json"
    out.write_text(record.model_dump_json(indent=2), encoding="utf-8")


def _aggregate_scores(records: list[ModelEvidenceRecord]) -> list[ModelModelScore]:
    by_model: dict[str, list[ModelEvidenceRecord]] = {}
    for rec in records:
        by_model.setdefault(rec.model_key, []).append(rec)

    scores: list[ModelModelScore] = []
    for model_key, recs in sorted(by_model.items()):
        extracted = [r for r in recs if r.extraction_success]
        evaluated = [r for r in recs if r.grading_success]
        grading_not_applicable = [r for r in recs if r.grading_not_applicable]
        failed = [
            r for r in recs if not r.grading_not_applicable and not r.grading_success
        ]

        def _avg(vals: list[float | None]) -> float | None:
            clean = [v for v in vals if v is not None]
            return sum(clean) / len(clean) if clean else None

        scores.append(
            ModelModelScore(
                model_key=model_key,
                entries_extracted=len(extracted),
                entries_evaluated=len(evaluated),
                entries_grading_not_applicable=len(grading_not_applicable),
                entries_failed=len(failed),
                avg_recall=_avg([r.recall for r in evaluated]),
                avg_precision=_avg([r.precision for r in evaluated]),
                avg_fidelity=_avg([r.fidelity for r in evaluated]),
                avg_format_compliance=_avg([r.format_compliance for r in evaluated]),
                total_latency_ms=sum(r.latency_ms for r in recs),
            )
        )

    return scores


__all__: list[str] = ["HandlerCanaryOrchestrator"]
