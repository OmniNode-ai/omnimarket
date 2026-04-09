"""Unit tests for scripts/generate_adapters.py."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Import the script under test via importlib (it lives in scripts/, not src/)
# ---------------------------------------------------------------------------
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "generate_adapters.py"
)

spec = importlib.util.spec_from_file_location("generate_adapters", _SCRIPT_PATH)
assert spec is not None
assert spec.loader is not None
_mod = importlib.util.module_from_spec(spec)
sys.modules["generate_adapters"] = _mod
spec.loader.exec_module(_mod)  # type: ignore[union-attr]

discover_orchestrator_nodes = _mod.discover_orchestrator_nodes
generate_adapters_for_node = _mod.generate_adapters_for_node
_render_skill_md = _mod._render_skill_md
_render_mdc = _mod._render_mdc
_render_instructions_md = _mod._render_instructions_md
_get_command_topic = _mod._get_command_topic
_get_completion_topic = _mod._get_completion_topic
_get_timeout_ms = _mod._get_timeout_ms
main = _mod.main


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_ORCHESTRATOR_METADATA = {
    "name": "node_test_orchestrator",
    "version": "1.0.0",
    "description": "A test orchestrator node",
    "node_role": "orchestrator",
    "display_name": "Test Orchestrator",
    "pack": "testing",
    "entry_flags": ["--dry-run", "--ticket"],
    "tags": ["test", "orchestrator"],
}

_COMPUTE_METADATA = {
    "name": "node_test_compute",
    "version": "1.0.0",
    "description": "A test compute node",
    "node_role": "compute",
    "tags": ["test"],
}

_NO_ROLE_METADATA = {
    "name": "node_test_no_role",
    "version": "1.0.0",
    "description": "A node without a role field",
    "tags": ["test"],
}

_CONTRACT = {
    "descriptor": {"timeout_ms": 60000},
    "event_bus": {
        "subscribe_topics": ["onex.cmd.omnimarket.test-start.v1"],
        "publish_topics": ["onex.evt.omnimarket.test-completed.v1"],
    },
    "terminal_event": "onex.evt.omnimarket.test-completed.v1",
}


def _write_node(
    root: Path,
    node_name: str,
    metadata: dict,
    contract: dict | None = None,
) -> Path:
    node_dir = root / node_name
    node_dir.mkdir(parents=True)
    (node_dir / "metadata.yaml").write_text(yaml.dump(metadata))
    if contract is not None:
        (node_dir / "contract.yaml").write_text(yaml.dump(contract))
    return node_dir


# ---------------------------------------------------------------------------
# Tests: discover_orchestrator_nodes
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDiscoverOrchestratorNodes:
    def test_returns_only_orchestrator_nodes(self, tmp_path: Path) -> None:
        _write_node(tmp_path, "node_orchestrator", _ORCHESTRATOR_METADATA, _CONTRACT)
        _write_node(tmp_path, "node_compute", _COMPUTE_METADATA, _CONTRACT)
        _write_node(tmp_path, "node_no_role", _NO_ROLE_METADATA, _CONTRACT)

        results = discover_orchestrator_nodes(tmp_path)
        assert len(results) == 1
        node_dir, metadata, _contract = results[0]
        assert node_dir.name == "node_orchestrator"
        assert metadata["node_role"] == "orchestrator"

    def test_skips_dunder_dirs(self, tmp_path: Path) -> None:
        _write_node(tmp_path, "__pycache__", _ORCHESTRATOR_METADATA)
        results = discover_orchestrator_nodes(tmp_path)
        assert len(results) == 0

    def test_skips_nodes_without_metadata(self, tmp_path: Path) -> None:
        (tmp_path / "node_no_meta").mkdir()
        results = discover_orchestrator_nodes(tmp_path)
        assert len(results) == 0

    def test_filter_by_node_name(self, tmp_path: Path) -> None:
        _write_node(tmp_path, "node_alpha", _ORCHESTRATOR_METADATA, _CONTRACT)
        _write_node(
            tmp_path,
            "node_beta",
            {**_ORCHESTRATOR_METADATA, "name": "node_beta"},
            _CONTRACT,
        )
        results = discover_orchestrator_nodes(tmp_path, filter_node="node_alpha")
        assert len(results) == 1
        assert results[0][0].name == "node_alpha"

    def test_contract_optional(self, tmp_path: Path) -> None:
        _write_node(tmp_path, "node_orchestrator", _ORCHESTRATOR_METADATA)
        results = discover_orchestrator_nodes(tmp_path)
        assert len(results) == 1
        _, _, contract = results[0]
        assert contract == {}

    def test_empty_nodes_dir(self, tmp_path: Path) -> None:
        results = discover_orchestrator_nodes(tmp_path)
        assert results == []


# ---------------------------------------------------------------------------
# Tests: helper functions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHelpers:
    def test_get_command_topic(self) -> None:
        assert _get_command_topic(_CONTRACT) == "onex.cmd.omnimarket.test-start.v1"

    def test_get_command_topic_missing(self) -> None:
        assert _get_command_topic({}) == "UNKNOWN_COMMAND_TOPIC"

    def test_get_completion_topic_prefers_terminal_event(self) -> None:
        assert (
            _get_completion_topic(_CONTRACT) == "onex.evt.omnimarket.test-completed.v1"
        )

    def test_get_completion_topic_falls_back_to_last_publish(self) -> None:
        contract = {
            "event_bus": {
                "publish_topics": [
                    "onex.evt.omnimarket.phase.v1",
                    "onex.evt.omnimarket.done.v1",
                ]
            }
        }
        assert _get_completion_topic(contract) == "onex.evt.omnimarket.done.v1"

    def test_get_completion_topic_missing(self) -> None:
        assert _get_completion_topic({}) == "UNKNOWN_COMPLETION_TOPIC"

    def test_get_timeout_ms(self) -> None:
        assert _get_timeout_ms(_CONTRACT) == 60000

    def test_get_timeout_ms_default(self) -> None:
        assert _get_timeout_ms({}) == 120000


# ---------------------------------------------------------------------------
# Tests: renderer functions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRenderers:
    _COMMON = {
        "node_name": "node_test_orchestrator",
        "slug": "test-orchestrator",
        "display_name": "Test Orchestrator",
        "description": "A test orchestrator node",
        "entry_flags": ["--dry-run", "--ticket"],
        "command_topic": "onex.cmd.omnimarket.test-start.v1",
        "completion_topic": "onex.evt.omnimarket.test-completed.v1",
        "timeout_ms": 60000,
    }

    def test_render_skill_md_contains_node_name(self) -> None:
        content = _render_skill_md(pack="testing", tags=["test"], **self._COMMON)
        assert "node_test_orchestrator" in content

    def test_render_skill_md_contains_topics(self) -> None:
        content = _render_skill_md(pack="testing", tags=["test"], **self._COMMON)
        assert "onex.cmd.omnimarket.test-start.v1" in content
        assert "onex.evt.omnimarket.test-completed.v1" in content

    def test_render_skill_md_contains_timeout(self) -> None:
        content = _render_skill_md(pack="testing", tags=["test"], **self._COMMON)
        assert "60000" in content

    def test_render_skill_md_contains_entry_flags(self) -> None:
        content = _render_skill_md(pack="testing", tags=["test"], **self._COMMON)
        assert "--dry-run" in content
        assert "--ticket" in content

    def test_render_skill_md_deterministic(self) -> None:
        c1 = _render_skill_md(pack="testing", tags=["test"], **self._COMMON)
        c2 = _render_skill_md(pack="testing", tags=["test"], **self._COMMON)
        assert c1 == c2

    def test_render_mdc_contains_topics(self) -> None:
        content = _render_mdc(**self._COMMON)
        assert "onex.cmd.omnimarket.test-start.v1" in content
        assert "onex.evt.omnimarket.test-completed.v1" in content

    def test_render_mdc_deterministic(self) -> None:
        c1 = _render_mdc(**self._COMMON)
        c2 = _render_mdc(**self._COMMON)
        assert c1 == c2

    def test_render_instructions_md_contains_node_name(self) -> None:
        content = _render_instructions_md(**self._COMMON)
        assert "node_test_orchestrator" in content

    def test_render_instructions_md_deterministic(self) -> None:
        c1 = _render_instructions_md(**self._COMMON)
        c2 = _render_instructions_md(**self._COMMON)
        assert c1 == c2

    def test_render_with_no_entry_flags(self) -> None:
        kwargs = {**self._COMMON, "entry_flags": []}
        content = _render_skill_md(pack="testing", tags=[], **kwargs)
        assert "No entry flags" in content

    def test_render_skill_md_has_valid_yaml_frontmatter(self) -> None:
        content = _render_skill_md(pack="testing", tags=["test"], **self._COMMON)
        # Extract frontmatter between first pair of ---
        parts = content.split("---")
        assert len(parts) >= 3
        frontmatter = yaml.safe_load(parts[1])
        assert frontmatter["version"] == "1.0.0"
        assert frontmatter["category"] == "testing"


# ---------------------------------------------------------------------------
# Tests: generate_adapters_for_node (integration of renderers + file writes)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGenerateAdaptersForNode:
    def test_dry_run_writes_no_files(self, tmp_path: Path) -> None:
        nodes_dir = tmp_path / "nodes"
        output_dir = tmp_path / "adapters"
        node_dir = _write_node(
            nodes_dir, "node_test_orchestrator", _ORCHESTRATOR_METADATA, _CONTRACT
        )
        _, metadata, contract = discover_orchestrator_nodes(nodes_dir)[0]

        paths = generate_adapters_for_node(
            node_dir=node_dir,
            metadata=metadata,
            contract=contract,
            output_dir=output_dir,
            dry_run=True,
        )

        assert not any(p.exists() for p in paths.values())

    def test_writes_all_three_files(self, tmp_path: Path) -> None:
        nodes_dir = tmp_path / "nodes"
        output_dir = tmp_path / "adapters"
        node_dir = _write_node(
            nodes_dir, "node_test_orchestrator", _ORCHESTRATOR_METADATA, _CONTRACT
        )
        _, metadata, contract = discover_orchestrator_nodes(nodes_dir)[0]

        paths = generate_adapters_for_node(
            node_dir=node_dir,
            metadata=metadata,
            contract=contract,
            output_dir=output_dir,
            dry_run=False,
        )

        assert paths["skill_md"].exists()
        assert paths["mdc"].exists()
        assert paths["instructions_md"].exists()

    def test_output_filenames(self, tmp_path: Path) -> None:
        nodes_dir = tmp_path / "nodes"
        output_dir = tmp_path / "adapters"
        node_dir = _write_node(
            nodes_dir, "node_test_orchestrator", _ORCHESTRATOR_METADATA, _CONTRACT
        )
        _, metadata, contract = discover_orchestrator_nodes(nodes_dir)[0]

        paths = generate_adapters_for_node(
            node_dir=node_dir,
            metadata=metadata,
            contract=contract,
            output_dir=output_dir,
        )

        assert paths["skill_md"].name == "test-orchestrator_SKILL.md"
        assert paths["mdc"].name == "test-orchestrator.mdc"
        assert paths["instructions_md"].name == "test-orchestrator-instructions.md"

    def test_deterministic_output(self, tmp_path: Path) -> None:
        """Same input always produces identical file content."""
        nodes_dir = tmp_path / "nodes"
        output_dir_a = tmp_path / "a"
        output_dir_b = tmp_path / "b"
        node_dir = _write_node(
            nodes_dir, "node_test_orchestrator", _ORCHESTRATOR_METADATA, _CONTRACT
        )
        _, metadata, contract = discover_orchestrator_nodes(nodes_dir)[0]

        paths_a = generate_adapters_for_node(
            node_dir=node_dir,
            metadata=metadata,
            contract=contract,
            output_dir=output_dir_a,
        )
        paths_b = generate_adapters_for_node(
            node_dir=node_dir,
            metadata=metadata,
            contract=contract,
            output_dir=output_dir_b,
        )

        for key in ("skill_md", "mdc", "instructions_md"):
            assert paths_a[key].read_text() == paths_b[key].read_text(), (
                f"{key} output is not deterministic"
            )

    def test_display_name_fallback(self, tmp_path: Path) -> None:
        """When display_name is absent from metadata, fall back to slug title-case."""
        meta = {**_ORCHESTRATOR_METADATA}
        del meta["display_name"]
        nodes_dir = tmp_path / "nodes"
        output_dir = tmp_path / "adapters"
        node_dir = _write_node(nodes_dir, "node_test_orchestrator", meta, _CONTRACT)
        _, metadata, contract = discover_orchestrator_nodes(nodes_dir)[0]

        paths = generate_adapters_for_node(
            node_dir=node_dir,
            metadata=metadata,
            contract=contract,
            output_dir=output_dir,
        )

        skill_content = paths["skill_md"].read_text()
        assert "Test Orchestrator" in skill_content


# ---------------------------------------------------------------------------
# Tests: main() CLI integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMainCLI:
    def test_main_dry_run_no_orchestrators(self, tmp_path: Path, capsys) -> None:
        nodes_dir = tmp_path / "nodes"
        nodes_dir.mkdir()
        _write_node(nodes_dir, "node_compute", _COMPUTE_METADATA, _CONTRACT)

        import generate_adapters as _ga

        original = _ga.NODES_DIR
        _ga.NODES_DIR = nodes_dir
        try:
            rc = main(["--dry-run"])
        finally:
            _ga.NODES_DIR = original

        assert rc == 0
        captured = capsys.readouterr()
        assert "No orchestrator nodes found" in captured.out

    def test_main_writes_files(self, tmp_path: Path) -> None:
        nodes_dir = tmp_path / "nodes"
        nodes_dir.mkdir()
        _write_node(nodes_dir, "node_orch", _ORCHESTRATOR_METADATA, _CONTRACT)
        output_dir = tmp_path / "out"

        import generate_adapters as _ga

        original = _ga.NODES_DIR
        _ga.NODES_DIR = nodes_dir
        try:
            rc = main(["--output-dir", str(output_dir)])
        finally:
            _ga.NODES_DIR = original

        assert rc == 0
        assert (output_dir / "claude_code" / "orch_SKILL.md").exists()
        assert (output_dir / "cursor" / "orch.mdc").exists()
        assert (output_dir / "codex" / "orch-instructions.md").exists()

    def test_main_dry_run_no_file_output(self, tmp_path: Path) -> None:
        nodes_dir = tmp_path / "nodes"
        nodes_dir.mkdir()
        _write_node(nodes_dir, "node_orch", _ORCHESTRATOR_METADATA, _CONTRACT)
        output_dir = tmp_path / "out"

        import generate_adapters as _ga

        original = _ga.NODES_DIR
        _ga.NODES_DIR = nodes_dir
        try:
            rc = main(["--dry-run", "--output-dir", str(output_dir)])
        finally:
            _ga.NODES_DIR = original

        assert rc == 0
        assert not output_dir.exists()
