"""Tests for the pure guidance section parser (COMPUTE-eligible, no filesystem I/O)."""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_provenance import (
    EnumContextPackProvenance,
)

from omnimarket.nodes.node_context_pack_builder_compute.parsers.parser_guidance_section import (
    GuidanceSectionParser,
)
from omnimarket.pack.model_context_pack_artifact import (
    ModelContextPackArtifact,
)


@pytest.mark.unit
class TestGuidanceSectionParser:
    """The parser is pure: string in → ParsedSection list out, no I/O."""

    def test_parses_top_level_headings(self) -> None:
        content = "# Alpha\n\nBody alpha.\n\n# Beta\n\nBody beta.\n"
        sections = GuidanceSectionParser().parse(content, source_file="guide.md")

        assert len(sections) == 2
        assert sections[0].heading_path == ("Alpha",)
        assert "Body alpha." in sections[0].content
        assert sections[1].heading_path == ("Beta",)
        assert "Body beta." in sections[1].content

    def test_parses_nested_headings(self) -> None:
        content = "# Parent\n\n## Child\n\nNested body.\n"
        sections = GuidanceSectionParser().parse(content, source_file="guide.md")

        assert len(sections) == 2
        parent, child = sections
        assert parent.heading_path == ("Parent",)
        assert child.heading_path == ("Parent", "Child")
        assert "Nested body." in child.content

    def test_source_file_is_preserved(self) -> None:
        content = "# Section\n\nContent.\n"
        sections = GuidanceSectionParser().parse(
            content, source_file="path/to/CLAUDE.md"
        )

        assert len(sections) == 1
        assert sections[0].source_file == "path/to/CLAUDE.md"

    def test_char_count_matches_section_content(self) -> None:
        content = "# Section\n\nContent here.\n"
        sections = GuidanceSectionParser().parse(content, source_file="guide.md")

        assert len(sections) == 1
        assert sections[0].char_count == len(sections[0].content)

    def test_reason_selected_default(self) -> None:
        content = "# Section\n\nContent.\n"
        sections = GuidanceSectionParser().parse(content, source_file="guide.md")

        assert len(sections) == 1
        assert sections[0].reason_selected == "heading_boundary"

    def test_custom_reason_selected(self) -> None:
        content = "# Section\n\nContent.\n"
        sections = GuidanceSectionParser().parse(
            content, source_file="guide.md", reason_selected="keyword_match"
        )

        assert len(sections) == 1
        assert sections[0].reason_selected == "keyword_match"

    def test_empty_document_returns_no_sections(self) -> None:
        sections = GuidanceSectionParser().parse("", source_file="guide.md")
        assert sections == []

    def test_document_with_no_headings_returns_no_sections(self) -> None:
        content = "Just plain text.\nNo headings here.\n"
        sections = GuidanceSectionParser().parse(content, source_file="guide.md")
        assert sections == []

    def test_content_hash_is_stable(self) -> None:
        content = "# Section\n\nContent.\n"
        p = GuidanceSectionParser()
        first = p.parse(content, source_file="guide.md")
        second = p.parse(content, source_file="guide.md")

        assert first[0].content_hash == second[0].content_hash

    def test_content_hash_differs_for_different_content(self) -> None:
        p = GuidanceSectionParser()
        a = p.parse("# S\n\nA.\n", source_file="f.md")
        b = p.parse("# S\n\nB.\n", source_file="f.md")

        assert a[0].content_hash != b[0].content_hash

    def test_to_artifact_produces_valid_model(self) -> None:
        content = "# Section\n\nContent.\n"
        section = GuidanceSectionParser().parse(content, source_file="guide.md")[0]
        artifact = section.to_artifact(
            factor=EnumContextFactor.CLAUDE_MD,
            token_estimate=15,
            provenance=EnumContextPackProvenance.CURATED,
            source_contract_hash="contract-abc",
        )

        assert isinstance(artifact, ModelContextPackArtifact)
        assert artifact.factor == EnumContextFactor.CLAUDE_MD
        assert artifact.token_estimate == 15
        assert artifact.provenance == EnumContextPackProvenance.CURATED
        assert artifact.source_file == "guide.md"
        assert artifact.heading_path == ("Section",)
        assert artifact.char_count == len(section.content)
        assert artifact.reason_selected == "heading_boundary"
        assert artifact.source_artifact_hash == section.content_hash
        assert artifact.source_contract_hash == "contract-abc"

    def test_multiple_sections_have_unique_content_hashes(self) -> None:
        content = "# Alpha\n\nBody alpha.\n\n# Beta\n\nBody beta.\n"
        sections = GuidanceSectionParser().parse(content, source_file="guide.md")

        hashes = [s.content_hash for s in sections]
        assert len(hashes) == len(set(hashes))

    def test_section_includes_heading_text_in_content(self) -> None:
        content = "# My Heading\n\nBody text.\n"
        sections = GuidanceSectionParser().parse(content, source_file="guide.md")

        assert "# My Heading" in sections[0].content


@pytest.mark.unit
class TestModelContextPackArtifactProvenanceFields:
    """New provenance fields are optional with None defaults for backwards compat."""

    def test_artifact_without_new_fields_still_valid(self) -> None:
        """Existing callers that don't supply new fields must not break."""
        artifact = ModelContextPackArtifact(
            factor=EnumContextFactor.GOLDEN_CHAIN,
            content="chain content",
            token_estimate=10,
            provenance=EnumContextPackProvenance.GENERATED,
            source_artifact_hash="sha-abc",
            source_contract_hash="contract-sha",
        )

        assert artifact.source_file is None
        assert artifact.heading_path is None
        assert artifact.char_count is None
        assert artifact.reason_selected is None

    def test_artifact_with_all_new_fields(self) -> None:
        artifact = ModelContextPackArtifact(
            factor=EnumContextFactor.CLAUDE_MD,
            content="section content",
            token_estimate=20,
            provenance=EnumContextPackProvenance.CURATED,
            source_artifact_hash="sha-xyz",
            source_contract_hash="contract-sha",
            source_file="CLAUDE.md",
            heading_path=["Parent", "Child"],
            char_count=15,
            reason_selected="heading_boundary",
        )

        assert artifact.source_file == "CLAUDE.md"
        assert artifact.heading_path == ("Parent", "Child")
        assert artifact.char_count == 15
        assert artifact.reason_selected == "heading_boundary"

    def test_heading_path_stored_as_tuple(self) -> None:
        """Frozen model requires immutable containers — heading_path is a tuple."""
        artifact = ModelContextPackArtifact(
            factor=EnumContextFactor.CLAUDE_MD,
            content="content",
            token_estimate=5,
            provenance=EnumContextPackProvenance.CURATED,
            source_artifact_hash="sha-h",
            source_contract_hash="contract-sha",
            heading_path=["A", "B"],
        )

        assert isinstance(artifact.heading_path, tuple)
        assert artifact.heading_path == ("A", "B")
