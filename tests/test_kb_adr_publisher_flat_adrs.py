# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""KB publisher writes flat adrs/ADR-NNNN-<slug>.md (OMN-14103, Gap 3).

The live knowledge-base convention is a flat ``adrs/`` directory with
``ADR-NNNN-<slug>.md`` files whose frontmatter matches
``schemas/frontmatter.schema.json`` (status: proposed). The publisher used to
write ``adrs/proposed/ADR-PROPOSED-NNNN-...`` which no live tool consumes. This
test runs the *real* renderer (only subprocess is mocked) and asserts the
on-disk layout matches the convention.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from omnimarket.models.adr import (
    EnumAdrKBDestination,
    ModelAdrSourceProvenance,
)
from omnimarket.nodes.node_kb_adr_publisher.handlers.handler_kb_adr_publisher import (
    HandlerKBADRPublisher,
    _next_adr_number,
)
from omnimarket.nodes.node_kb_adr_publisher.models.model_publish_request import (
    ModelKBADRPublishRequest,
)

_SOURCE_PROVENANCE = ModelAdrSourceProvenance(
    source_repository="OmniNode-ai/omnimarket",
    source_visibility="public",
    publication_classification="public",
)


def _decision(idx: int) -> dict[str, Any]:
    return {
        "draft": {
            "status": "Proposed",
            "date": "2026-07-06T10:00:00+00:00",
            "title": f"Adopt pattern number {idx}",
            "context": "Some context.",
            "decision": "Do the thing.",
            "consequences": "Trade-offs apply.",
            "alternatives_considered": [],
            "supersedes": [],
            "source_evidence": ["seg-1"],
            "extraction_metadata": {
                "model_id": "qwen3-coder-local",
                "confidence": 0.9,
                "pipeline_version": "1.0.0",
                "prompt_template_id": "t",
                "prompt_template_version": "1.0",
                "canary_run_id": "canary-run-1",
                "extracted_at": "2026-07-06T10:00:00+00:00",
            },
        },
        "source_provenance": _SOURCE_PROVENANCE.model_dump(mode="json"),
        "kb_destination": "public",
        "source_documents": [
            {"source_path": "docs/source.md", "source_content_sha256": "a" * 64}
        ],
    }


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "canary-run-1"
    d.mkdir(parents=True)
    (d / "extracted_decisions.json").write_text(
        json.dumps([_decision(1), _decision(2)]), encoding="utf-8"
    )
    return d


@pytest.mark.unit
def test_next_adr_number_continues_from_existing(tmp_path: Path) -> None:
    adrs = tmp_path / "adrs"
    adrs.mkdir()
    assert _next_adr_number(adrs) == 1
    (adrs / "ADR-0001-a.md").write_text("x")
    (adrs / "ADR-0007-b.md").write_text("x")
    (adrs / "README.md").write_text("x")  # non-ADR file ignored
    assert _next_adr_number(adrs) == 8


@pytest.mark.unit
async def test_publisher_writes_flat_adrs_with_sequential_numbers(
    run_dir: Path,
) -> None:
    captured: dict[str, str] = {}
    subdirs_seen: list[str] = []

    def fake_subprocess(cmd: list[str], **kwargs: Any) -> MagicMock:
        if "clone" in cmd:
            kb = Path(cmd[-1])
            (kb / ".git").mkdir(parents=True, exist_ok=True)
            # Seed an existing ADR so numbering must continue from ADR-0006.
            adrs = kb / "adrs"
            adrs.mkdir(parents=True, exist_ok=True)
            (adrs / "ADR-0005-existing-decision.md").write_text(
                "---\ntype: adr\n---\n", encoding="utf-8"
            )
        if "add" in cmd and "-C" in cmd:
            kb = Path(cmd[cmd.index("-C") + 1])
            adrs = kb / "adrs"
            for f in sorted(adrs.rglob("*")):
                if f.is_file():
                    rel = f.relative_to(adrs).as_posix()
                    captured[rel] = f.read_text(encoding="utf-8")
                if f.is_dir():
                    subdirs_seen.append(f.relative_to(adrs).as_posix())
        m = MagicMock()
        m.stdout = "https://github.com/OmniNode-ai/knowledge-base/pull/1\n"
        m.returncode = 0
        return m

    with patch("subprocess.run", side_effect=fake_subprocess):
        result = await HandlerKBADRPublisher().handle(
            ModelKBADRPublishRequest(
                canary_run_dir=str(run_dir),
                model_key="qwen3-coder-local",
                kb_destination=EnumAdrKBDestination.public,
                source_provenance=_SOURCE_PROVENANCE,
            )
        )

    assert result.success is True
    assert result.adr_count == 2

    md_files = sorted(n for n in captured if n.endswith(".md"))
    # Flat layout — no adrs/proposed/ subdirectory.
    assert "proposed" not in subdirs_seen
    assert all("/" not in name for name in md_files), md_files
    # Numbering continues from the seeded ADR-0005 -> ADR-0006, ADR-0007.
    assert "ADR-0006-adopt-pattern-number-1.md" in md_files
    assert "ADR-0007-adopt-pattern-number-2.md" in md_files

    # Frontmatter matches the ADRFrontmatter convention (status: proposed).
    body = captured["ADR-0006-adopt-pattern-number-1.md"]
    fm = yaml.safe_load(body.split("---\n", maxsplit=2)[1])
    assert fm["type"] == "adr"
    assert fm["status"] == "proposed"
    assert fm["adr_id"] == "ADR-0006"
    assert "ADR-0006" in fm["title"]
