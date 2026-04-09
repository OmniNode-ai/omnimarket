"""Unit tests for scripts/generate_catalog.py.

Tests use fixture metadata.yaml files under tests/fixtures/catalog_nodes/.

Fixtures:
  node_public_a   -- pack=pack_alpha, node_role=public, has display_name
  node_public_b   -- pack=pack_beta, node_role=public, no display_name
  node_internal_c -- pack=pack_alpha, node_role=internal (excluded)
  node_deprecated_d -- pack=pack_alpha, deprecated=true with deprecation info
  node_no_pack_e  -- no pack field (goes to 'uncategorized')
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# Add scripts/ to path so we can import generate_catalog
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import generate_catalog  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "catalog_nodes"


@pytest.fixture
def fixture_root() -> Path:
    return FIXTURES_DIR


class TestFindMetadataFiles:
    def test_finds_all_yaml_files(self, fixture_root: Path) -> None:
        files = generate_catalog.find_metadata_files(fixture_root)
        names = [f.parent.name for f in files]
        assert "node_public_a" in names
        assert "node_public_b" in names
        assert "node_internal_c" in names
        assert "node_deprecated_d" in names
        assert "node_no_pack_e" in names

    def test_returns_sorted_list(self, fixture_root: Path) -> None:
        files = generate_catalog.find_metadata_files(fixture_root)
        assert files == sorted(files)

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        assert generate_catalog.find_metadata_files(tmp_path) == []


class TestLoadMetadata:
    def test_loads_valid_yaml(self, fixture_root: Path) -> None:
        path = fixture_root / "node_public_a" / "metadata.yaml"
        data = generate_catalog.load_metadata(path)
        assert data is not None
        assert data["name"] == "node_public_a"

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        result = generate_catalog.load_metadata(tmp_path / "missing.yaml")
        assert result is None

    def test_returns_none_for_invalid_yaml(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("{ unclosed: [")
        result = generate_catalog.load_metadata(bad)
        assert result is None


class TestIsInternal:
    def test_internal_role_returns_true(self) -> None:
        assert generate_catalog.is_internal({"node_role": "internal"}) is True

    def test_internal_uppercase_returns_true(self) -> None:
        assert generate_catalog.is_internal({"node_role": "INTERNAL"}) is True

    def test_public_role_returns_false(self) -> None:
        assert generate_catalog.is_internal({"node_role": "public"}) is False

    def test_missing_role_returns_false(self) -> None:
        assert generate_catalog.is_internal({}) is False


class TestIsDeprecated:
    def test_deprecated_true_returns_true(self) -> None:
        assert generate_catalog.is_deprecated({"deprecated": True}) is True

    def test_deprecated_false_returns_false(self) -> None:
        assert generate_catalog.is_deprecated({"deprecated": False}) is False

    def test_missing_deprecated_returns_false(self) -> None:
        assert generate_catalog.is_deprecated({}) is False


class TestGetDeprecationInfo:
    def test_full_deprecation_info(self) -> None:
        metadata = {
            "deprecated": True,
            "deprecation_reason": "too slow",
            "deprecation_since": "1.0.0",
            "replacement": "node_fast",
        }
        info = generate_catalog.get_deprecation_info(metadata)
        assert info is not None
        assert info["reason"] == "too slow"
        assert info["since"] == "1.0.0"
        assert info["replacement"] == "node_fast"

    def test_not_deprecated_returns_none(self) -> None:
        assert generate_catalog.get_deprecation_info({"deprecated": False}) is None

    def test_deprecated_with_no_extra_fields(self) -> None:
        info = generate_catalog.get_deprecation_info({"deprecated": True})
        assert info == {}


class TestGroupNodesByPack:
    def test_groups_by_pack(self) -> None:
        nodes = [
            {"name": "a", "pack": "alpha"},
            {"name": "b", "pack": "beta"},
            {"name": "c", "pack": "alpha"},
        ]
        groups = generate_catalog.group_nodes_by_pack(nodes)
        assert "alpha" in groups
        assert "beta" in groups
        assert len(groups["alpha"]) == 2
        assert len(groups["beta"]) == 1

    def test_no_pack_goes_to_uncategorized(self) -> None:
        nodes = [{"name": "x", "pack": "uncategorized"}]
        groups = generate_catalog.group_nodes_by_pack(nodes)
        assert "uncategorized" in groups

    def test_nodes_sorted_by_name_within_pack(self) -> None:
        nodes = [
            {"name": "z_node", "pack": "alpha"},
            {"name": "a_node", "pack": "alpha"},
        ]
        groups = generate_catalog.group_nodes_by_pack(nodes)
        names = [n["name"] for n in groups["alpha"]]
        assert names == sorted(names)

    def test_packs_sorted_alphabetically(self) -> None:
        nodes = [
            {"name": "x", "pack": "zeta"},
            {"name": "y", "pack": "alpha"},
        ]
        groups = generate_catalog.group_nodes_by_pack(nodes)
        assert list(groups.keys()) == sorted(groups.keys())


@pytest.mark.unit
class TestRunFunction:
    def test_run_produces_catalog_yaml(
        self, fixture_root: Path, tmp_path: Path
    ) -> None:
        rc = generate_catalog.run(fixture_root, tmp_path)
        assert rc == 0
        catalog_path = tmp_path / "catalog.yaml"
        assert catalog_path.exists()
        with open(catalog_path) as f:
            data = yaml.safe_load(f)
        assert data["schema_version"] == "1.0"
        assert "packs" in data

    def test_run_produces_catalog_md(self, fixture_root: Path, tmp_path: Path) -> None:
        rc = generate_catalog.run(fixture_root, tmp_path)
        assert rc == 0
        md_path = tmp_path / "catalog.md"
        assert md_path.exists()
        content = md_path.read_text()
        assert "# OmniMarket Node Catalog" in content

    def test_internal_nodes_excluded(self, fixture_root: Path, tmp_path: Path) -> None:
        generate_catalog.run(fixture_root, tmp_path)
        with open(tmp_path / "catalog.yaml") as f:
            data = yaml.safe_load(f)
        all_node_names = [
            node["name"] for pack in data["packs"] for node in pack["nodes"]
        ]
        assert "node_internal_c" not in all_node_names

    def test_public_nodes_included(self, fixture_root: Path, tmp_path: Path) -> None:
        generate_catalog.run(fixture_root, tmp_path)
        with open(tmp_path / "catalog.yaml") as f:
            data = yaml.safe_load(f)
        all_node_names = [
            node["name"] for pack in data["packs"] for node in pack["nodes"]
        ]
        assert "node_public_a" in all_node_names
        assert "node_public_b" in all_node_names

    def test_deprecated_node_flagged(self, fixture_root: Path, tmp_path: Path) -> None:
        generate_catalog.run(fixture_root, tmp_path)
        with open(tmp_path / "catalog.yaml") as f:
            data = yaml.safe_load(f)
        all_nodes = {
            node["name"]: node for pack in data["packs"] for node in pack["nodes"]
        }
        assert "node_deprecated_d" in all_nodes
        assert all_nodes["node_deprecated_d"]["deprecated"] is True
        assert "deprecation_info" in all_nodes["node_deprecated_d"]
        info = all_nodes["node_deprecated_d"]["deprecation_info"]
        assert info["replacement"] == "node_public_a"

    def test_no_pack_node_in_uncategorized(
        self, fixture_root: Path, tmp_path: Path
    ) -> None:
        generate_catalog.run(fixture_root, tmp_path)
        with open(tmp_path / "catalog.yaml") as f:
            data = yaml.safe_load(f)
        pack_names = [p["name"] for p in data["packs"]]
        assert "uncategorized" in pack_names
        uncategorized_nodes = next(
            p["nodes"] for p in data["packs"] if p["name"] == "uncategorized"
        )
        uncategorized_names = [n["name"] for n in uncategorized_nodes]
        assert "node_no_pack_e" in uncategorized_names

    def test_idempotent(self, fixture_root: Path, tmp_path: Path) -> None:
        """Running twice produces identical output."""
        generate_catalog.run(fixture_root, tmp_path)
        yaml_first = (tmp_path / "catalog.yaml").read_text()
        md_first = (tmp_path / "catalog.md").read_text()

        generate_catalog.run(fixture_root, tmp_path)
        yaml_second = (tmp_path / "catalog.yaml").read_text()
        md_second = (tmp_path / "catalog.md").read_text()

        assert yaml_first == yaml_second
        assert md_first == md_second

    def test_grouped_by_pack_in_yaml(self, fixture_root: Path, tmp_path: Path) -> None:
        generate_catalog.run(fixture_root, tmp_path)
        with open(tmp_path / "catalog.yaml") as f:
            data = yaml.safe_load(f)
        pack_map = {p["name"]: p["nodes"] for p in data["packs"]}
        assert "pack_alpha" in pack_map
        assert "pack_beta" in pack_map
        alpha_names = [n["name"] for n in pack_map["pack_alpha"]]
        assert "node_public_a" in alpha_names
        assert "node_deprecated_d" in alpha_names
        # internal node excluded even though it's in pack_alpha
        assert "node_internal_c" not in alpha_names

    def test_md_contains_deprecated_marker(
        self, fixture_root: Path, tmp_path: Path
    ) -> None:
        generate_catalog.run(fixture_root, tmp_path)
        content = (tmp_path / "catalog.md").read_text()
        assert "deprecated" in content.lower()

    def test_creates_output_dir_if_missing(
        self, fixture_root: Path, tmp_path: Path
    ) -> None:
        nested = tmp_path / "deep" / "nested"
        rc = generate_catalog.run(fixture_root, nested)
        assert rc == 0
        assert (nested / "catalog.yaml").exists()
