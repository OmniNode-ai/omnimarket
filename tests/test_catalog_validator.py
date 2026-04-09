"""Tests for the catalog completeness validator (scripts/validate_catalog.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.validate_catalog import (
    check_catalog_fresh,
    check_node_naming,
    check_pack_fields,
    find_metadata_files,
    run,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_metadata(tmp_path: Path, name: str, data: dict) -> Path:
    """Write a metadata.yaml file under tmp_path/nodes/<name>/."""
    node_dir = tmp_path / "nodes" / name
    node_dir.mkdir(parents=True)
    meta = node_dir / "metadata.yaml"
    meta.write_text(yaml.dump(data))
    return meta


# ---------------------------------------------------------------------------
# find_metadata_files
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFindMetadataFiles:
    def test_finds_files_under_nodes_dir(self, tmp_path: Path) -> None:
        _write_metadata(
            tmp_path, "node_foo_bar", {"name": "node_foo_bar", "version": "1.0.0"}
        )
        _write_metadata(
            tmp_path, "node_baz_qux", {"name": "node_baz_qux", "version": "1.0.0"}
        )
        files = find_metadata_files(tmp_path)
        assert len(files) == 2

    def test_empty_directory(self, tmp_path: Path) -> None:
        (tmp_path / "nodes").mkdir()
        files = find_metadata_files(tmp_path)
        assert files == []

    def test_falls_back_to_root_when_no_nodes_dir(self, tmp_path: Path) -> None:
        meta = tmp_path / "metadata.yaml"
        meta.write_text(yaml.dump({"name": "node_x_y", "version": "1.0.0"}))
        files = find_metadata_files(tmp_path)
        assert meta in files


# ---------------------------------------------------------------------------
# check_pack_fields — compliant fixtures
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCheckPackFieldsCompliant:
    def test_node_with_pack_passes(self, tmp_path: Path) -> None:
        _write_metadata(
            tmp_path,
            "node_core_orchestrator",
            {"name": "node_core_orchestrator", "version": "1.0.0", "pack": "core"},
        )
        files = find_metadata_files(tmp_path)
        errors = check_pack_fields(files)
        assert errors == []

    def test_multiple_nodes_all_with_pack(self, tmp_path: Path) -> None:
        for i, pack in enumerate(["core", "pipeline", "review"]):
            _write_metadata(
                tmp_path,
                f"node_{pack}_effect_{i}",
                {"name": f"node_{pack}_effect_{i}", "version": "1.0.0", "pack": pack},
            )
        files = find_metadata_files(tmp_path)
        errors = check_pack_fields(files)
        assert errors == []


# ---------------------------------------------------------------------------
# check_pack_fields — non-compliant fixtures
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCheckPackFieldsNonCompliant:
    def test_node_missing_pack_fails(self, tmp_path: Path) -> None:
        _write_metadata(
            tmp_path,
            "node_orphan_compute",
            {"name": "node_orphan_compute", "version": "1.0.0"},
        )
        files = find_metadata_files(tmp_path)
        errors = check_pack_fields(files)
        assert len(errors) == 1
        assert "MISSING_PACK" in errors[0]
        assert "node_orphan_compute" in errors[0]

    def test_node_with_null_pack_fails(self, tmp_path: Path) -> None:
        meta_path = _write_metadata(
            tmp_path,
            "node_null_pack_compute",
            {"name": "node_null_pack_compute", "version": "1.0.0"},
        )
        # Overwrite with explicit null
        meta_path.write_text(
            "name: node_null_pack_compute\nversion: '1.0.0'\npack: null\n"
        )
        files = find_metadata_files(tmp_path)
        errors = check_pack_fields(files)
        assert len(errors) == 1
        assert "MISSING_PACK" in errors[0]

    def test_node_with_empty_string_pack_fails(self, tmp_path: Path) -> None:
        _write_metadata(
            tmp_path,
            "node_empty_pack_compute",
            {"name": "node_empty_pack_compute", "version": "1.0.0", "pack": ""},
        )
        files = find_metadata_files(tmp_path)
        errors = check_pack_fields(files)
        assert len(errors) == 1
        assert "MISSING_PACK" in errors[0]

    def test_error_message_includes_remediation_hint(self, tmp_path: Path) -> None:
        _write_metadata(
            tmp_path,
            "node_ungrouped_reducer",
            {"name": "node_ungrouped_reducer", "version": "1.0.0"},
        )
        files = find_metadata_files(tmp_path)
        errors = check_pack_fields(files)
        assert len(errors) == 1
        assert "pack:" in errors[0]

    def test_parse_error_reported(self, tmp_path: Path) -> None:
        node_dir = tmp_path / "nodes" / "node_broken_node"
        node_dir.mkdir(parents=True)
        (node_dir / "metadata.yaml").write_text(": invalid: yaml: ::::")
        files = find_metadata_files(tmp_path)
        errors = check_pack_fields(files)
        assert any("PARSE_ERROR" in e for e in errors)

    def test_multiple_nodes_partial_missing(self, tmp_path: Path) -> None:
        _write_metadata(
            tmp_path,
            "node_core_effect",
            {"name": "node_core_effect", "version": "1.0.0", "pack": "core"},
        )
        _write_metadata(
            tmp_path,
            "node_orphan_compute",
            {"name": "node_orphan_compute", "version": "1.0.0"},
        )
        files = find_metadata_files(tmp_path)
        errors = check_pack_fields(files)
        assert len(errors) == 1
        assert "node_orphan_compute" in errors[0]


# ---------------------------------------------------------------------------
# check_node_naming — compliant fixtures
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCheckNodeNamingCompliant:
    def test_two_segment_name_passes(self, tmp_path: Path) -> None:
        _write_metadata(
            tmp_path,
            "node_core_orchestrator",
            {"name": "node_core_orchestrator", "version": "1.0.0"},
        )
        files = find_metadata_files(tmp_path)
        errors = check_node_naming(files)
        assert errors == []

    def test_three_segment_name_with_qualifier_passes(self, tmp_path: Path) -> None:
        _write_metadata(
            tmp_path,
            "node_pipeline_effect_closeout",
            {"name": "node_pipeline_effect_closeout", "version": "1.0.0"},
        )
        files = find_metadata_files(tmp_path)
        errors = check_node_naming(files)
        assert errors == []

    def test_multi_qualifier_name_passes(self, tmp_path: Path) -> None:
        _write_metadata(
            tmp_path,
            "node_pr_lifecycle_inventory_compute",
            {"name": "node_pr_lifecycle_inventory_compute", "version": "1.0.0"},
        )
        files = find_metadata_files(tmp_path)
        errors = check_node_naming(files)
        assert errors == []


# ---------------------------------------------------------------------------
# check_node_naming — non-compliant fixtures
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCheckNodeNamingNonCompliant:
    def test_name_without_node_prefix_fails(self, tmp_path: Path) -> None:
        _write_metadata(
            tmp_path,
            "some_handler",
            {"name": "some_handler", "version": "1.0.0"},
        )
        files = find_metadata_files(tmp_path)
        errors = check_node_naming(files)
        assert len(errors) == 1
        assert "NAMING_CONVENTION" in errors[0]
        assert "node_" in errors[0]

    def test_name_with_only_node_prefix_fails(self, tmp_path: Path) -> None:
        _write_metadata(
            tmp_path,
            "node_singlepart",
            {"name": "node_singlepart", "version": "1.0.0"},
        )
        files = find_metadata_files(tmp_path)
        errors = check_node_naming(files)
        assert len(errors) == 1
        assert "NAMING_CONVENTION" in errors[0]

    def test_error_message_includes_convention(self, tmp_path: Path) -> None:
        _write_metadata(
            tmp_path,
            "handler_foo",
            {"name": "handler_foo", "version": "1.0.0"},
        )
        files = find_metadata_files(tmp_path)
        errors = check_node_naming(files)
        assert len(errors) == 1
        assert "node_<pack>_<role>" in errors[0]


# ---------------------------------------------------------------------------
# check_catalog_fresh
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCheckCatalogFresh:
    def test_missing_catalog_reports_error(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "catalog" / "catalog.yaml"
        errors = check_catalog_fresh(
            nonexistent, tmp_path, tmp_path / "generate_catalog.py"
        )
        assert len(errors) == 1
        assert "CATALOG_MISSING" in errors[0]
        assert "generate_catalog" in errors[0]

    def test_missing_generator_skips_check(self, tmp_path: Path) -> None:
        catalog_path = tmp_path / "catalog.yaml"
        catalog_path.write_text("schema_version: '1.0'\npacks: []\n")
        errors = check_catalog_fresh(
            catalog_path, tmp_path, tmp_path / "nonexistent_script.py"
        )
        assert errors == []


# ---------------------------------------------------------------------------
# run() integration — using temp fixtures
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunIntegration:
    def test_all_compliant_returns_zero(self, tmp_path: Path) -> None:
        _write_metadata(
            tmp_path,
            "node_core_orchestrator",
            {"name": "node_core_orchestrator", "version": "1.0.0", "pack": "core"},
        )
        result = run(
            root=tmp_path,
            catalog_path=None,
            check_pack=True,
            check_naming=True,
            check_catalog=False,
        )
        assert result == 0

    def test_missing_pack_returns_one(self, tmp_path: Path) -> None:
        _write_metadata(
            tmp_path,
            "node_orphan_effect",
            {"name": "node_orphan_effect", "version": "1.0.0"},
        )
        result = run(
            root=tmp_path,
            catalog_path=None,
            check_pack=True,
            check_naming=True,
            check_catalog=False,
        )
        assert result == 1

    def test_naming_violation_returns_one(self, tmp_path: Path) -> None:
        _write_metadata(
            tmp_path,
            "bad_handler",
            {"name": "bad_handler", "version": "1.0.0", "pack": "core"},
        )
        result = run(
            root=tmp_path,
            catalog_path=None,
            check_pack=True,
            check_naming=True,
            check_catalog=False,
        )
        assert result == 1

    def test_empty_nodes_dir_returns_zero(self, tmp_path: Path) -> None:
        (tmp_path / "nodes").mkdir()
        result = run(
            root=tmp_path,
            catalog_path=None,
            check_pack=True,
            check_naming=True,
            check_catalog=False,
        )
        assert result == 0

    def test_check_pack_only_flag(self, tmp_path: Path) -> None:
        # Node has correct pack but naming violation — pack-only check should pass
        _write_metadata(
            tmp_path,
            "bad_handler",
            {"name": "bad_handler", "version": "1.0.0", "pack": "core"},
        )
        result = run(
            root=tmp_path,
            catalog_path=None,
            check_pack=True,
            check_naming=False,
            check_catalog=False,
        )
        assert result == 0

    def test_check_naming_only_flag(self, tmp_path: Path) -> None:
        # Node missing pack but correct naming — naming-only check should pass
        _write_metadata(
            tmp_path,
            "node_core_effect",
            {"name": "node_core_effect", "version": "1.0.0"},
        )
        result = run(
            root=tmp_path,
            catalog_path=None,
            check_pack=False,
            check_naming=True,
            check_catalog=False,
        )
        assert result == 0
