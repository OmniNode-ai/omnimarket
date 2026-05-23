# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for kb_adr_renderer — ModelADRDraft to knowledge-base format."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from omnibase_core.models.adr.model_adr_draft import ModelADRDraft
from omnibase_core.models.adr.model_adr_extraction_metadata import (
    ModelADRExtractionMetadata,
)

from omnimarket.adapters.adr.kb_adr_renderer import (
    ModelKBRenderResult,
    render_adr_to_kb,
)


@pytest.fixture
def sample_metadata() -> ModelADRExtractionMetadata:
    return ModelADRExtractionMetadata(
        model_id="qwen3-coder-30b",
        confidence=0.87,
        pipeline_version="1.2.0",
        prompt_template_id="adr-extraction-v3",
        prompt_template_version="3.0.1",
        canary_run_id="canary-2026-05-23-001",
        extracted_at=datetime(2026, 5, 23, 10, 0, 0, tzinfo=UTC),
    )


@pytest.fixture
def sample_draft(sample_metadata: ModelADRExtractionMetadata) -> ModelADRDraft:
    return ModelADRDraft(
        date=datetime(2026, 5, 23, tzinfo=UTC),
        title="Use Pydantic for all wire DTOs",
        context="We have inconsistent DTO definitions across repos.",
        decision="All wire DTOs must be Pydantic BaseModel subclasses.",
        consequences="Easier validation; heavier import overhead at startup.",
        alternatives_considered=["dataclasses", "attrs", "TypedDict"],
        supersedes=["ADR-0001"],
        source_evidence=["seg-abc123", "seg-def456"],
        extraction_metadata=sample_metadata,
    )


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    return tmp_path / "adrs"


def test_render_creates_both_files(
    sample_draft: ModelADRDraft, output_dir: Path
) -> None:
    output_dir.mkdir(parents=True)
    result = render_adr_to_kb(sample_draft, "ADR-0042", output_dir)

    assert isinstance(result, ModelKBRenderResult)
    assert result.adr_path.exists()
    assert result.evidence_path.exists()


def test_frontmatter_required_fields(
    sample_draft: ModelADRDraft, output_dir: Path
) -> None:
    output_dir.mkdir(parents=True)
    result = render_adr_to_kb(sample_draft, "ADR-0042", output_dir)

    raw = result.adr_path.read_text(encoding="utf-8")
    # Extract YAML frontmatter between --- delimiters
    parts = raw.split("---\n", maxsplit=2)
    assert len(parts) >= 3, "Markdown should have YAML frontmatter"
    fm = yaml.safe_load(parts[1])

    assert fm["type"] == "adr"
    assert fm["adr_id"] == "ADR-0042"
    assert "title" in fm
    assert "date" in fm
    assert "topics" in fm
    assert "refs" in fm
    assert "supersedes" in fm
    assert "superseded_by" in fm


def test_status_always_proposed(sample_draft: ModelADRDraft, output_dir: Path) -> None:
    output_dir.mkdir(parents=True)
    result = render_adr_to_kb(sample_draft, "ADR-0042", output_dir)

    raw = result.adr_path.read_text(encoding="utf-8")
    parts = raw.split("---\n", maxsplit=2)
    fm = yaml.safe_load(parts[1])

    assert fm["status"] == "proposed"


def test_frontmatter_title_contains_adr_id(
    sample_draft: ModelADRDraft, output_dir: Path
) -> None:
    output_dir.mkdir(parents=True)
    result = render_adr_to_kb(sample_draft, "ADR-0042", output_dir)

    raw = result.adr_path.read_text(encoding="utf-8")
    parts = raw.split("---\n", maxsplit=2)
    fm = yaml.safe_load(parts[1])

    assert "ADR-0042" in fm["title"]
    assert "Use Pydantic for all wire DTOs" in fm["title"]


def test_markdown_body_contains_all_sections(
    sample_draft: ModelADRDraft, output_dir: Path
) -> None:
    output_dir.mkdir(parents=True)
    result = render_adr_to_kb(sample_draft, "ADR-0042", output_dir)

    body = result.adr_path.read_text(encoding="utf-8")
    assert "## Context" in body
    assert "## Decision" in body
    assert "## Consequences" in body
    assert "## Alternatives Considered" in body
    assert "## Evidence" in body


def test_alternatives_rendered_as_numbered_list(
    sample_draft: ModelADRDraft, output_dir: Path
) -> None:
    output_dir.mkdir(parents=True)
    result = render_adr_to_kb(sample_draft, "ADR-0042", output_dir)

    body = result.adr_path.read_text(encoding="utf-8")
    assert "1. dataclasses" in body
    assert "2. attrs" in body
    assert "3. TypedDict" in body


def test_evidence_json_contains_extraction_metadata(
    sample_draft: ModelADRDraft, output_dir: Path
) -> None:
    output_dir.mkdir(parents=True)
    result = render_adr_to_kb(sample_draft, "ADR-0042", output_dir)

    evidence = json.loads(result.evidence_path.read_text(encoding="utf-8"))

    assert evidence["adr_id"] == "ADR-0042"
    assert evidence["extraction_model_id"] == "qwen3-coder-30b"
    assert evidence["confidence"] == pytest.approx(0.87)
    assert evidence["pipeline_version"] == "1.2.0"
    assert evidence["prompt_template_id"] == "adr-extraction-v3"
    assert evidence["prompt_template_version"] == "3.0.1"
    assert evidence["canary_run_id"] == "canary-2026-05-23-001"
    assert evidence["source_segment_ids"] == ["seg-abc123", "seg-def456"]
    assert "extracted_at" in evidence


def test_supersedes_in_frontmatter(
    sample_draft: ModelADRDraft, output_dir: Path
) -> None:
    output_dir.mkdir(parents=True)
    result = render_adr_to_kb(sample_draft, "ADR-0042", output_dir)

    raw = result.adr_path.read_text(encoding="utf-8")
    parts = raw.split("---\n", maxsplit=2)
    fm = yaml.safe_load(parts[1])

    assert fm["supersedes"] == ["ADR-0001"]
    assert fm["superseded_by"] == []


def test_no_alternatives_section_when_empty(
    sample_metadata: ModelADRExtractionMetadata, output_dir: Path
) -> None:
    draft = ModelADRDraft(
        date=datetime(2026, 5, 23, tzinfo=UTC),
        title="Minimal ADR",
        context="Context.",
        decision="Decision.",
        consequences="Consequences.",
        extraction_metadata=sample_metadata,
    )
    output_dir.mkdir(parents=True)
    result = render_adr_to_kb(draft, "ADR-0001", output_dir)

    body = result.adr_path.read_text(encoding="utf-8")
    assert "## Alternatives Considered" not in body


def test_slug_in_filename(sample_draft: ModelADRDraft, output_dir: Path) -> None:
    output_dir.mkdir(parents=True)
    result = render_adr_to_kb(sample_draft, "ADR-0042", output_dir)

    assert "adr-0042" in result.adr_path.name
    assert result.adr_path.suffix == ".md"
    assert result.evidence_path.suffix == ".json"
    assert "adr-0042" in result.evidence_path.name
