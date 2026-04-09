"""Unit tests for the multi-host adapter generator script.

Verifies that generate_adapter.py produces correct Gemini CLI adapter output
given fixture metadata.yaml and contract.yaml files.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# Add scripts/ to path so we can import generate_adapter directly
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import generate_adapter  # noqa: E402

FIXTURE_METADATA_ORCHESTRATOR = {
    "name": "node_test_orchestrator",
    "version": "1.0.0",
    "node_role": "orchestrator",
    "display_name": "Test Orchestrator",
    "description": "Runs end-to-end test orchestration.",
    "pack": "omnimarket",
    "entry_flags": ["--dry-run"],
}

FIXTURE_METADATA_NON_ORCHESTRATOR = {
    "name": "node_test_compute",
    "version": "1.0.0",
    "node_role": "compute",
    "display_name": "Test Compute",
    "description": "A compute node.",
    "pack": "omnimarket",
}

FIXTURE_CONTRACT = {
    "name": "test_orchestrator",
    "contract_version": {"major": 1, "minor": 0, "patch": 0},
    "descriptor": {
        "timeout_ms": 90000,
    },
    "inputs": {
        "dry_run": {
            "type": "bool",
            "description": "Report only, no side effects",
            "default": False,
        },
        "repos": {
            "type": "list[str]",
            "description": "Target repositories",
            "default": "all",
        },
    },
    "event_bus": {
        "subscribe_topics": ["onex.cmd.omnimarket.test-orchestrator-start.v1"],
        "publish_topics": ["onex.evt.omnimarket.test-orchestrator-completed.v1"],
    },
}


@pytest.fixture
def node_dir(tmp_path: Path) -> Path:
    """Create a temporary orchestrator node directory with fixture files."""
    nd = tmp_path / "node_test_orchestrator"
    nd.mkdir()
    (nd / "metadata.yaml").write_text(yaml.dump(FIXTURE_METADATA_ORCHESTRATOR))
    (nd / "contract.yaml").write_text(yaml.dump(FIXTURE_CONTRACT))
    return nd


@pytest.fixture
def non_orchestrator_node_dir(tmp_path: Path) -> Path:
    """Create a temporary compute node directory (should be skipped by generator)."""
    nd = tmp_path / "node_test_compute"
    nd.mkdir()
    (nd / "metadata.yaml").write_text(yaml.dump(FIXTURE_METADATA_NON_ORCHESTRATOR))
    (nd / "contract.yaml").write_text(yaml.dump(FIXTURE_CONTRACT))
    return nd


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    out = tmp_path / "adapters"
    out.mkdir()
    return out


class TestBuildSubstitutions:
    def test_uses_display_name_from_metadata(self) -> None:
        subs = generate_adapter._build_substitutions(
            "node_test_orchestrator",
            FIXTURE_METADATA_ORCHESTRATOR,
            FIXTURE_CONTRACT,
        )
        assert subs["SKILL_DISPLAY_NAME"] == "Test Orchestrator"

    def test_falls_back_to_derived_display_name(self) -> None:
        metadata = {**FIXTURE_METADATA_ORCHESTRATOR, "display_name": None}
        subs = generate_adapter._build_substitutions(
            "node_test_orchestrator", metadata, FIXTURE_CONTRACT
        )
        assert subs["SKILL_DISPLAY_NAME"] == "Test Orchestrator"

    def test_command_topic_extracted(self) -> None:
        subs = generate_adapter._build_substitutions(
            "node_test_orchestrator", FIXTURE_METADATA_ORCHESTRATOR, FIXTURE_CONTRACT
        )
        assert subs["COMMAND_TOPIC"] == "onex.cmd.omnimarket.test-orchestrator-start.v1"

    def test_completion_topic_extracted(self) -> None:
        subs = generate_adapter._build_substitutions(
            "node_test_orchestrator", FIXTURE_METADATA_ORCHESTRATOR, FIXTURE_CONTRACT
        )
        assert (
            subs["COMPLETION_TOPIC"]
            == "onex.evt.omnimarket.test-orchestrator-completed.v1"
        )

    def test_timeout_ms_extracted(self) -> None:
        subs = generate_adapter._build_substitutions(
            "node_test_orchestrator", FIXTURE_METADATA_ORCHESTRATOR, FIXTURE_CONTRACT
        )
        assert subs["TIMEOUT_MS"] == "90000"

    def test_skill_slug_derived_from_node_name(self) -> None:
        subs = generate_adapter._build_substitutions(
            "node_test_orchestrator", FIXTURE_METADATA_ORCHESTRATOR, FIXTURE_CONTRACT
        )
        assert subs["SKILL_SLUG"] == "test-orchestrator"

    def test_pack_used_as_category(self) -> None:
        subs = generate_adapter._build_substitutions(
            "node_test_orchestrator", FIXTURE_METADATA_ORCHESTRATOR, FIXTURE_CONTRACT
        )
        assert subs["CATEGORY"] == "omnimarket"


class TestApplySubstitutions:
    def test_replaces_placeholders(self) -> None:
        template = "Hello {{NAME}}, welcome to {{PLACE}}."
        result = generate_adapter._apply_substitutions(
            template, {"NAME": "Alice", "PLACE": "OmniMarket"}
        )
        assert result == "Hello Alice, welcome to OmniMarket."

    def test_leaves_unknown_placeholders_intact(self) -> None:
        template = "Hello {{NAME}}, {{UNKNOWN}}."
        result = generate_adapter._apply_substitutions(template, {"NAME": "Alice"})
        assert "{{UNKNOWN}}" in result

    def test_deterministic(self) -> None:
        template = "{{A}} {{B}}"
        subs = {"A": "foo", "B": "bar"}
        assert generate_adapter._apply_substitutions(
            template, subs
        ) == generate_adapter._apply_substitutions(template, subs)


class TestOutputFilename:
    def test_gemini_uses_md_extension(self) -> None:
        assert (
            generate_adapter._output_filename("gemini", "aislop-sweep")
            == "aislop-sweep.md"
        )

    def test_claude_code_uses_skill_md(self) -> None:
        assert (
            generate_adapter._output_filename("claude_code", "aislop-sweep")
            == "aislop_sweep_SKILL.md"
        )

    def test_cursor_uses_mdc_extension(self) -> None:
        assert (
            generate_adapter._output_filename("cursor", "aislop-sweep")
            == "aislop-sweep.mdc"
        )

    def test_codex_uses_instructions_md(self) -> None:
        assert (
            generate_adapter._output_filename("codex", "aislop-sweep")
            == "aislop-sweep-instructions.md"
        )


class TestGenerateAdaptersGemini:
    def test_generates_gemini_md_file(self, node_dir: Path, output_dir: Path) -> None:
        generated = generate_adapter.generate_adapters(
            node_dir, output_dir, formats=("gemini",)
        )
        assert len(generated) == 1
        out_file = generated[0]
        assert out_file.name == "test-orchestrator.md"
        assert out_file.exists()

    def test_gemini_output_contains_display_name(
        self, node_dir: Path, output_dir: Path
    ) -> None:
        generate_adapter.generate_adapters(node_dir, output_dir, formats=("gemini",))
        content = (output_dir / "gemini" / "test-orchestrator.md").read_text()
        assert "Test Orchestrator" in content

    def test_gemini_output_contains_command_topic(
        self, node_dir: Path, output_dir: Path
    ) -> None:
        generate_adapter.generate_adapters(node_dir, output_dir, formats=("gemini",))
        content = (output_dir / "gemini" / "test-orchestrator.md").read_text()
        assert "onex.cmd.omnimarket.test-orchestrator-start.v1" in content

    def test_gemini_output_contains_completion_topic(
        self, node_dir: Path, output_dir: Path
    ) -> None:
        generate_adapter.generate_adapters(node_dir, output_dir, formats=("gemini",))
        content = (output_dir / "gemini" / "test-orchestrator.md").read_text()
        assert "onex.evt.omnimarket.test-orchestrator-completed.v1" in content

    def test_gemini_output_contains_timeout(
        self, node_dir: Path, output_dir: Path
    ) -> None:
        generate_adapter.generate_adapters(node_dir, output_dir, formats=("gemini",))
        content = (output_dir / "gemini" / "test-orchestrator.md").read_text()
        assert "90000" in content

    def test_gemini_output_is_deterministic(
        self, node_dir: Path, tmp_path: Path
    ) -> None:
        out1 = tmp_path / "out1"
        out2 = tmp_path / "out2"
        out1.mkdir()
        out2.mkdir()
        generate_adapter.generate_adapters(node_dir, out1, formats=("gemini",))
        generate_adapter.generate_adapters(node_dir, out2, formats=("gemini",))
        file1 = (out1 / "gemini" / "test-orchestrator.md").read_text()
        file2 = (out2 / "gemini" / "test-orchestrator.md").read_text()
        assert file1 == file2

    def test_non_orchestrator_node_skipped(
        self, non_orchestrator_node_dir: Path, output_dir: Path
    ) -> None:
        generated = generate_adapter.generate_adapters(
            non_orchestrator_node_dir, output_dir, formats=("gemini",)
        )
        assert generated == []

    def test_missing_metadata_skipped(self, tmp_path: Path, output_dir: Path) -> None:
        nd = tmp_path / "node_no_meta"
        nd.mkdir()
        (nd / "contract.yaml").write_text(yaml.dump(FIXTURE_CONTRACT))
        generated = generate_adapter.generate_adapters(
            nd, output_dir, formats=("gemini",)
        )
        assert generated == []

    def test_all_formats_generated(self, node_dir: Path, output_dir: Path) -> None:
        generated = generate_adapter.generate_adapters(node_dir, output_dir)
        assert len(generated) == 4
        names = {p.name for p in generated}
        assert "test-orchestrator.md" in names
        assert "test_orchestrator_SKILL.md" in names
        assert "test-orchestrator.mdc" in names
        assert "test-orchestrator-instructions.md" in names


class TestExtractArgsTable:
    def test_produces_table_rows_for_inputs(self) -> None:
        table = generate_adapter._extract_args_table(FIXTURE_CONTRACT)
        assert "dry_run" in table
        assert "repos" in table

    def test_empty_inputs_returns_placeholder(self) -> None:
        table = generate_adapter._extract_args_table({})
        assert "no arguments" in table


class TestMainCli:
    def test_main_with_node_flag(
        self, node_dir: Path, output_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Point NODES_DIR at the temp node parent; ADAPTERS_DIR stays real so templates resolve.
        monkeypatch.setattr(generate_adapter, "NODES_DIR", node_dir.parent)
        result = generate_adapter.main(
            [
                "--node",
                "node_test_orchestrator",
                "--output-dir",
                str(output_dir),
                "--formats",
                "gemini",
            ]
        )
        assert result == 0
        assert (output_dir / "gemini" / "test-orchestrator.md").exists()

    def test_main_formats_gemini_only(
        self, node_dir: Path, output_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(generate_adapter, "NODES_DIR", node_dir.parent)
        generate_adapter.main(["--output-dir", str(output_dir), "--formats", "gemini"])
        out_file = output_dir / "gemini" / "test-orchestrator.md"
        assert out_file.exists()
