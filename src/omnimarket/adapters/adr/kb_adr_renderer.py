# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Render ModelADRDraft into knowledge-base frontmatter + markdown format."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from omnibase_core.models.adr.model_adr_draft import ModelADRDraft


@dataclass(frozen=True, slots=True)
class ModelKBRenderResult:
    """Paths written by the renderer."""

    adr_path: Path
    evidence_path: Path


def render_adr_to_kb(
    draft: ModelADRDraft,
    adr_id: str,
    output_dir: Path,
) -> ModelKBRenderResult:
    """Transform a ModelADRDraft into KB-formatted markdown + evidence JSON.

    Writes two files under output_dir:
    - <slug>.md  — YAML frontmatter + markdown body
    - <slug>-evidence.json  — extraction provenance metadata

    Returns the paths of both written files.
    """
    slug = _slug(adr_id, draft.title)

    adr_path = output_dir / f"{slug}.md"
    evidence_path = output_dir / f"{slug}-evidence.json"

    adr_path.write_text(_render_markdown(draft, adr_id), encoding="utf-8")
    evidence_path.write_text(_render_evidence(draft, adr_id), encoding="utf-8")

    return ModelKBRenderResult(adr_path=adr_path, evidence_path=evidence_path)


def _slug(adr_id: str, title: str) -> str:
    id_part = adr_id.lower().replace(" ", "-")
    title_part = re.sub(r"[^a-z0-9-]", "-", title.lower())[:50].strip("-")
    title_part = re.sub(r"-{2,}", "-", title_part)
    return f"{id_part}-{title_part}"


def _render_markdown(draft: ModelADRDraft, adr_id: str) -> str:
    frontmatter = {
        "type": "adr",
        "status": "proposed",
        "date": draft.date.strftime("%Y-%m-%d"),
        "title": f"{adr_id}: {draft.title}",
        "adr_id": adr_id,
        "topics": list(draft.source_evidence),  # segment IDs used as topic refs
        "refs": [],
        "supersedes": list(draft.supersedes),
        "superseded_by": [],
    }

    fm_str = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)
    header = f"---\n{fm_str}---\n\n"

    sections: list[str] = [f"# {adr_id}: {draft.title}", ""]

    sections += ["## Context", "", draft.context, ""]
    sections += ["## Decision", "", draft.decision, ""]

    if draft.alternatives_considered:
        sections.append("## Alternatives Considered")
        sections.append("")
        for i, alt in enumerate(draft.alternatives_considered, 1):
            sections.append(f"{i}. {alt}")
        sections.append("")

    sections += ["## Consequences", "", draft.consequences, ""]

    meta = draft.extraction_metadata
    sections += [
        "## Evidence",
        "",
        f"Extracted by ADR canary pipeline run `{meta.canary_run_id}` "
        f"using model `{meta.model_id}` (confidence {meta.confidence:.2f}). "
        "See companion evidence JSON for full provenance.",
        "",
    ]

    return header + "\n".join(sections)


def _render_evidence(draft: ModelADRDraft, adr_id: str) -> str:
    meta = draft.extraction_metadata
    evidence = {
        "adr_id": adr_id,
        "extraction_model_id": meta.model_id,
        "confidence": meta.confidence,
        "pipeline_version": meta.pipeline_version,
        "prompt_template_id": meta.prompt_template_id,
        "prompt_template_version": meta.prompt_template_version,
        "canary_run_id": meta.canary_run_id,
        "source_segment_ids": list(draft.source_evidence),
        "extracted_at": meta.extracted_at.isoformat(),
    }
    return json.dumps(evidence, indent=2)


__all__ = ["ModelKBRenderResult", "render_adr_to_kb"]
