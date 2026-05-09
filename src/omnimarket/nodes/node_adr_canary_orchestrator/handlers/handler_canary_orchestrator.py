# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerCanaryOrchestrator — drives the ADR canary evaluation pipeline.

Pipeline per manifest entry:
  1. Ingest source documents (HandlerDocumentIngestion, injected)
  2. For each model: extract decisions (HandlerDecisionExtraction, injected, concurrent)
  3. Grade each extraction (HandlerExtractionGrader, injected)
  4. Generate ADR draft (HandlerAdrGeneration, injected)
  5. Write evidence JSON to output_dir/<run_id>/<entry_id>/<model_key>.json
  6. Write scorecard.md to output_dir/<run_id>/scorecard.md
  7. Emit adr-canary-completed.v1

All sub-handlers are injected; defaults call sibling node handlers directly.
Config is read from contract.yaml config block via from_contract() factory.

[OMN-10698]
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import string
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_adr_canary_orchestrator.models.model_canary_report import (
    ModelCanaryReport,
    ModelModelScore,
)
from omnimarket.nodes.node_adr_canary_orchestrator.models.model_canary_request import (
    ModelCanaryCommandPayload,
)

logger = logging.getLogger(__name__)

# onex-topic-allow: pending contract auto-wiring
TOPIC_CANARY_COMPLETED = "onex.evt.omnimarket.adr-canary-completed.v1"


# ---------------------------------------------------------------------------
# Protocols for injected sub-handlers
# ---------------------------------------------------------------------------


@runtime_checkable
class ProtocolIngestionHandler(Protocol):
    async def handle(self, *, request: Any) -> Any: ...


@runtime_checkable
class ProtocolExtractionHandler(Protocol):
    async def handle(self, request: Any) -> Any: ...


@runtime_checkable
class ProtocolGraderHandler(Protocol):
    async def handle(self, request: Any) -> Any: ...


@runtime_checkable
class ProtocolDraftGenHandler(Protocol):
    async def handle(self, request: Any) -> Any: ...


# ---------------------------------------------------------------------------
# Ground truth manifest models
# ---------------------------------------------------------------------------


class ModelManifestModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    provider: str = "local"
    model_id: str
    external: bool = False


class ModelManifestEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    root_paths: list[str]
    ground_truth_adr: str
    models: list[ModelManifestModel]


class ModelGroundTruthManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: list[ModelManifestEntry]


# ---------------------------------------------------------------------------
# Evidence record written per model per entry
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

    All sub-handlers are optional; when omitted, the sibling node handlers are
    imported lazily so the module does not force circular imports at load time.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effectful"] = "effectful"

    def __init__(
        self,
        ingestion_handler: ProtocolIngestionHandler | None = None,
        extraction_handler: ProtocolExtractionHandler | None = None,
        grader_handler: ProtocolGraderHandler | None = None,
        draft_gen_handler: ProtocolDraftGenHandler | None = None,
        max_concurrent_extractions: int = 4,
        grader_model_key: str = "opus",
        allow_external_providers: bool = False,
    ) -> None:
        self._ingestion_handler = ingestion_handler
        self._extraction_handler = extraction_handler
        self._grader_handler = grader_handler
        self._draft_gen_handler = draft_gen_handler
        self._max_concurrent_extractions = max_concurrent_extractions
        self._grader_model_key = grader_model_key
        self._allow_external_providers = allow_external_providers

    # ------------------------------------------------------------------
    # Sub-handler lazy defaults
    # ------------------------------------------------------------------

    def _get_ingestion_handler(self) -> ProtocolIngestionHandler:
        if self._ingestion_handler is not None:
            return self._ingestion_handler
        from omnimarket.nodes.node_adr_document_ingestion_effect.handlers.handler_document_ingestion import (
            HandlerDocumentIngestion,
        )

        return HandlerDocumentIngestion()

    def _get_extraction_handler(self) -> ProtocolExtractionHandler:
        if self._extraction_handler is not None:
            return self._extraction_handler
        from omnimarket.nodes.node_adr_decision_extraction_llm_effect.handlers.handler_decision_extraction import (
            HandlerDecisionExtraction,
        )

        return HandlerDecisionExtraction()

    def _get_grader_handler(self) -> ProtocolGraderHandler:
        if self._grader_handler is not None:
            return self._grader_handler
        from omnimarket.nodes.node_adr_extraction_grader_llm_effect.handlers.handler_extraction_grader import (
            HandlerExtractionGrader,
        )

        return HandlerExtractionGrader(grader_model_key=self._grader_model_key)

    def _get_draft_gen_handler(self) -> ProtocolDraftGenHandler:
        if self._draft_gen_handler is not None:
            return self._draft_gen_handler
        from omnimarket.nodes.node_adr_draft_generation_compute.handlers.handler_adr_generation import (
            HandlerAdrGeneration,
        )

        return HandlerAdrGeneration()

    # ------------------------------------------------------------------
    # Manifest loading
    # ------------------------------------------------------------------

    def _load_manifest(self, manifest_path: str) -> ModelGroundTruthManifest:
        path = Path(manifest_path)
        if not path.is_absolute():
            omni_home = os.environ.get("OMNI_HOME", "")
            if omni_home:
                candidate = Path(omni_home) / "omnimarket" / manifest_path
                if candidate.exists():
                    path = candidate
        with path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        return ModelGroundTruthManifest.model_validate(raw)

    # ------------------------------------------------------------------
    # Per-model extraction + grading
    # ------------------------------------------------------------------

    async def _run_model(
        self,
        entry: ModelManifestEntry,
        model: ModelManifestModel,
        run_id: str,
        ingestion_result: Any,
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

        extraction_handler = self._get_extraction_handler()
        grader_handler = self._get_grader_handler()
        draft_gen_handler = self._get_draft_gen_handler()

        # Step 1: extract
        try:
            from omnimarket.nodes.node_adr_decision_extraction_llm_effect.models.model_extraction_request import (
                ModelExtractionRequest,
            )

            extraction_req = ModelExtractionRequest(
                documents=ingestion_result.documents,
                model_key=model.key,
                model_id=model.model_id,
                correlation_id=run_id,
            )
            extraction_result = await extraction_handler.handle(extraction_req)
            extraction_success = getattr(extraction_result, "success", False)
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

        if not extraction_success:
            err = getattr(extraction_result, "error_message", "extraction failed")
            record = record.model_copy(
                update={
                    "extraction_error": err,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                }
            )
            _write_evidence(evidence_entry_dir, model.key, record)
            return record

        record = record.model_copy(update={"extraction_success": True})

        # Step 2: grade
        try:
            from omnimarket.nodes.node_adr_extraction_grader_llm_effect.models.model_grading_request import (
                ModelGradingRequest,
            )

            extractions = getattr(extraction_result, "extractions", [])
            extraction_dicts = [
                e.model_dump() if hasattr(e, "model_dump") else dict(e)
                for e in extractions
            ]
            grading_req = ModelGradingRequest(
                ground_truth_adr=entry.ground_truth_adr,
                extraction_output=extraction_dicts,
                source_document="\n\n".join(
                    getattr(doc, "source_path", "")
                    for doc in ingestion_result.documents
                ),
                correlation_id=run_id,
                model_key_under_test=model.key,
            )
            grading_result = await grader_handler.handle(grading_req)
            grading_success = getattr(grading_result, "success", False)
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

        if grading_success:
            record = record.model_copy(
                update={
                    "grading_success": True,
                    "recall": getattr(grading_result, "recall", None),
                    "precision": getattr(grading_result, "precision", None),
                    "fidelity": getattr(grading_result, "fidelity", None),
                    "format_compliance": getattr(
                        grading_result, "format_compliance", None
                    ),
                }
            )

        # Step 3: generate ADR draft
        try:
            from omnimarket.nodes.node_adr_draft_generation_compute.models.model_generation_request import (
                ModelADRGenerationRequest,
            )

            if extractions:
                first_extraction = extractions[0]
                draft_req = ModelADRGenerationRequest(
                    extraction=first_extraction,
                    run_id=run_id,
                )
                draft_result = await draft_gen_handler.handle(draft_req)
                draft_ok = getattr(draft_result, "status", "error") == "ok"
                if draft_ok:
                    draft_path = evidence_entry_dir / f"{model.key}_draft.md"
                    draft_path.write_text(
                        getattr(draft_result, "markdown", ""), encoding="utf-8"
                    )
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
    # Main handle
    # ------------------------------------------------------------------

    async def handle(self, request: ModelCanaryCommandPayload) -> ModelCanaryReport:
        run_id = request.resume_run_id or _make_run_id()
        logger.info(
            "adr-canary started (run_id=%s, dry_run=%s)", run_id, request.dry_run
        )

        if request.dry_run:
            logger.info("dry_run=True — loading manifest only, no LLM calls")
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

        # Load manifest
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

        ingestion_handler = self._get_ingestion_handler()
        sem = asyncio.Semaphore(self._max_concurrent_extractions)

        all_records: list[ModelEvidenceRecord] = []
        entries_completed = 0
        entries_failed = 0

        for entry in manifest.entries:
            evidence_entry_dir = evidence_dir / entry.id
            evidence_entry_dir.mkdir(parents=True, exist_ok=True)

            # Step 1: ingest documents for this entry
            try:
                from omnimarket.nodes.node_adr_document_ingestion_effect.models.model_ingestion_request import (
                    ModelIngestionRequest,
                )

                ingest_req = ModelIngestionRequest(root_paths=entry.root_paths)
                ingestion_result = await ingestion_handler.handle(request=ingest_req)
            except Exception as exc:
                logger.error("Ingestion failed for entry %s: %s", entry.id, exc)
                entries_failed += 1
                continue

            # Filter models by subset if specified
            models_to_run = entry.models
            if request.model_subset:
                models_to_run = [
                    m for m in entry.models if m.key in request.model_subset
                ]

            # Step 2-4: run models concurrently (bounded by semaphore)
            async def _bounded_run(
                _entry: ModelManifestEntry = entry,
                _model: ModelManifestModel = None,  # type: ignore[assignment]
                _ingestion: Any = ingestion_result,
                _evidence_dir: Path = evidence_entry_dir,
            ) -> ModelEvidenceRecord:
                async with sem:
                    return await self._run_model(
                        _entry, _model, run_id, _ingestion, _evidence_dir
                    )

            tasks = [asyncio.create_task(_bounded_run(_model=m)) for m in models_to_run]
            entry_records = await asyncio.gather(*tasks, return_exceptions=True)

            entry_ok = True
            for rec in entry_records:
                if isinstance(rec, BaseException):
                    logger.error("Model task raised: %s", rec)
                    entry_ok = False
                else:
                    all_records.append(rec)

            if entry_ok:
                entries_completed += 1
            else:
                entries_failed += 1

        # Aggregate per-model scores
        model_scores = _aggregate_scores(all_records)

        # Write scorecard
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
            "adr-canary complete (run_id=%s, entries=%d/%d, scorecard=%s)",
            run_id,
            entries_completed,
            len(manifest.entries),
            scorecard_path,
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
        cfg = contract.get("config", {})
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
