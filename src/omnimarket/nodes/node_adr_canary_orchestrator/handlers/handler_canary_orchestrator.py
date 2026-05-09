# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerCanaryOrchestrator — drives the ADR canary evaluation pipeline.

Pipeline per manifest entry:
  1. Ingest source documents via ProtocolAdrIngestion
  2. For each model: extract via ProtocolAdrExtraction (concurrent, bounded)
  3. Grade each extraction via ProtocolAdrGrading
  4. Generate ADR draft via ProtocolAdrDraftGen
  5. Write evidence JSON to output_dir/<run_id>/<entry_id>/<model_key>.json
  6. Write scorecard.md to output_dir/<run_id>/scorecard.md

All protocols use shared ADR types from omnimarket.models.adr — no sibling
node package imports. Sub-node adapters translate shared → private models.
Topics are read from contract.yaml, never hardcoded.

[OMN-10698]
"""

from __future__ import annotations

import asyncio
import logging
import random
import string
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, ConfigDict

from omnimarket.models.adr import (
    ModelAdrExtractionSummary,
    ModelAdrGradingScores,
    ModelAdrIngestionResult,
    ModelAdrManifestEntry,
    ModelAdrManifestModel,
)
from omnimarket.nodes.node_adr_canary_orchestrator.models.model_canary_report import (
    ModelCanaryReport,
    ModelModelScore,
)
from omnimarket.nodes.node_adr_canary_orchestrator.models.model_canary_request import (
    ModelCanaryCommandPayload,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol interfaces — defined by what the orchestrator needs, not by what
# the sub-nodes expose. Adapters (in tests or wired by runtime) translate.
# ---------------------------------------------------------------------------


@runtime_checkable
class ProtocolAdrIngestion(Protocol):
    """Consume root_paths; return document references."""

    async def ingest(self, root_paths: list[str]) -> ModelAdrIngestionResult: ...


@runtime_checkable
class ProtocolAdrExtraction(Protocol):
    """Extract decisions from an ingestion result for a specific model."""

    async def extract(
        self,
        *,
        ingestion: ModelAdrIngestionResult,
        model_key: str,
        model_id: str,
        correlation_id: str,
    ) -> ModelAdrExtractionSummary: ...


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
    draft_generated: bool = False
    extraction_error: str | None = None
    grading_error: str | None = None
    latency_ms: int = 0


# ---------------------------------------------------------------------------
# Ground truth manifest (orchestrator-owned types)
# ---------------------------------------------------------------------------


class ModelGroundTruthManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: list[ModelAdrManifestEntry]


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
        "| Model | Entries | Recall | Precision | Fidelity | Format | Latency (ms) |",
        "|-------|---------|--------|-----------|----------|--------|--------------|",
    ]

    def _fmt(v: float | None) -> str:
        return f"{v:.3f}" if v is not None else "—"

    for score in sorted(
        scores,
        key=lambda s: (s.avg_recall or 0.0) + (s.avg_precision or 0.0),
        reverse=True,
    ):
        lines.append(
            f"| {score.model_key} | {score.entries_evaluated} "
            f"| {_fmt(score.avg_recall)} | {_fmt(score.avg_precision)} "
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

    def __init__(
        self,
        ingestion_handler: ProtocolAdrIngestion | None = None,
        extraction_handler: ProtocolAdrExtraction | None = None,
        grader_handler: ProtocolAdrGrading | None = None,
        draft_gen_handler: ProtocolAdrDraftGen | None = None,
        max_concurrent_extractions: int = 4,
        grader_model_key: str = "opus",
        allow_external_providers: bool = False,
        topic_completed: str = "",
    ) -> None:
        self._ingestion = ingestion_handler
        self._extraction = extraction_handler
        self._grading = grader_handler
        self._draft_gen = draft_gen_handler
        self._max_concurrent_extractions = max_concurrent_extractions
        self._grader_model_key = grader_model_key
        self._allow_external_providers = allow_external_providers
        self._topic_completed = topic_completed

    # ------------------------------------------------------------------
    # Default protocol adapters (imported lazily to avoid circular deps)
    # ------------------------------------------------------------------

    def _get_ingestion(self) -> ProtocolAdrIngestion:
        if self._ingestion is not None:
            return self._ingestion
        from omnimarket.nodes.node_adr_canary_orchestrator.adapters.adapter_ingestion import (
            AdapterAdrIngestion,
        )

        return AdapterAdrIngestion()

    def _get_extraction(self) -> ProtocolAdrExtraction:
        if self._extraction is not None:
            return self._extraction
        from omnimarket.nodes.node_adr_canary_orchestrator.adapters.adapter_extraction import (
            AdapterAdrExtraction,
        )

        return AdapterAdrExtraction()

    def _get_grading(self) -> ProtocolAdrGrading:
        if self._grading is not None:
            return self._grading
        from omnimarket.nodes.node_adr_canary_orchestrator.adapters.adapter_grading import (
            AdapterAdrGrading,
        )

        return AdapterAdrGrading()

    def _get_draft_gen(self) -> ProtocolAdrDraftGen:
        if self._draft_gen is not None:
            return self._draft_gen
        from omnimarket.nodes.node_adr_canary_orchestrator.adapters.adapter_draft_gen import (
            AdapterAdrDraftGen,
        )

        return AdapterAdrDraftGen()

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
        evidence_entry_dir: Path,
    ) -> ModelEvidenceRecord:
        if model.external and not self._allow_external_providers:
            logger.info(
                "Skipping external model %s (allow_external_providers=False)", model.key
            )
            return ModelEvidenceRecord(
                run_id=run_id,
                entry_id=entry.id,
                model_key=model.key,
                model_id=model.model_id,
                extraction_error="external_provider_disabled",
            )

        t0 = time.monotonic()
        record = ModelEvidenceRecord(
            run_id=run_id,
            entry_id=entry.id,
            model_key=model.key,
            model_id=model.model_id,
        )
        extraction_proto = self._get_extraction()
        grading_proto = self._get_grading()
        draft_gen_proto = self._get_draft_gen()

        # Step 1: extract
        try:
            extraction = await extraction_proto.extract(
                ingestion=ingestion,
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
            return record

        if not extraction.success:
            record = record.model_copy(
                update={
                    "extraction_error": extraction.error_message or "extraction failed",
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                }
            )
            _write_evidence(evidence_entry_dir, model.key, record)
            return record

        record = record.model_copy(update={"extraction_success": True})

        # Step 2: grade
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
                "Grading failed (entry=%s, model=%s): %s", entry.id, model.key, exc
            )
            record = record.model_copy(
                update={
                    "grading_error": f"{type(exc).__name__}: {exc}",
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                }
            )
            _write_evidence(evidence_entry_dir, model.key, record)
            return record

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
        return record

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

        evidence_dir = Path(request.output_dir) / run_id
        evidence_dir.mkdir(parents=True, exist_ok=True)

        ingestion_proto = self._get_ingestion()
        sem = asyncio.Semaphore(self._max_concurrent_extractions)

        all_records: list[ModelEvidenceRecord] = []
        entries_completed = 0
        entries_failed = 0

        for entry in manifest.entries:
            evidence_entry_dir = evidence_dir / entry.id
            evidence_entry_dir.mkdir(parents=True, exist_ok=True)

            # Ingest documents for this entry
            try:
                ingestion_result = await ingestion_proto.ingest(
                    root_paths=entry.root_paths
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

            # Run models concurrently (bounded by semaphore)
            async def _bounded(
                _entry: ModelAdrManifestEntry = entry,
                _model: ModelAdrManifestModel = None,  # type: ignore[assignment]
                _ing: ModelAdrIngestionResult = ingestion_result,
                _edir: Path = evidence_entry_dir,
            ) -> ModelEvidenceRecord:
                async with sem:
                    return await self._run_model(_entry, _model, run_id, _ing, _edir)

            tasks = [asyncio.create_task(_bounded(_model=m)) for m in models_to_run]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            entry_ok = True
            for rec in results:
                if isinstance(rec, BaseException):
                    logger.error("Model task raised: %s", rec)
                    entry_ok = False
                else:
                    all_records.append(rec)

            if entry_ok:
                entries_completed += 1
            else:
                entries_failed += 1

        model_scores = _aggregate_scores(all_records)
        scorecard_path = _write_scorecard(
            run_id=run_id,
            manifest_path=request.manifest_path,
            scores=model_scores,
            evidence_dir=evidence_dir,
            entries_total=len(manifest.entries),
            entries_completed=entries_completed,
            entries_failed=entries_failed,
        )

        logger.info(
            "adr-canary complete (run_id=%s, entries=%d/%d)",
            run_id,
            entries_completed,
            len(manifest.entries),
        )

        return ModelCanaryReport(
            run_id=run_id,
            manifest_path=request.manifest_path,
            entries_total=len(manifest.entries),
            entries_completed=entries_completed,
            entries_failed=entries_failed,
            model_scores=model_scores,
            evidence_dir=str(evidence_dir),
            scorecard_path=str(scorecard_path),
        )

    @classmethod
    def from_contract(cls, contract: dict[str, Any]) -> HandlerCanaryOrchestrator:
        """Build from a loaded contract.yaml dict."""
        cfg = contract.get("config", {})
        event_bus = contract.get("event_bus", {})
        publish_topics: list[str] = event_bus.get("publish_topics", [])
        topic_completed = next((t for t in publish_topics if "completed" in t), "")
        return cls(
            max_concurrent_extractions=int(
                cfg.get("max_concurrent_extractions", {}).get("default", 4)
            ),
            grader_model_key=str(
                cfg.get("grader_model_key", {}).get("default", "opus")
            ),
            allow_external_providers=bool(
                cfg.get("allow_external_providers", {}).get("default", False)
            ),
            topic_completed=topic_completed,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_id() -> str:
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{ts}-{suffix}"


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
        evaluated = [r for r in recs if r.grading_success]
        failed = [r for r in recs if not r.grading_success]

        def _avg(vals: list[float | None]) -> float | None:
            clean = [v for v in vals if v is not None]
            return sum(clean) / len(clean) if clean else None

        scores.append(
            ModelModelScore(
                model_key=model_key,
                entries_evaluated=len(evaluated),
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
