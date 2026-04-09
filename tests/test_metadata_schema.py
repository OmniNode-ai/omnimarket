"""Test metadata.yaml schema validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_metadata import MetadataSchema

_REPO_ROOT = Path(__file__).parent.parent
_NODES_DIR = _REPO_ROOT / "src" / "omnimarket" / "nodes"


@pytest.mark.unit
class TestMetadataSchema:
    """Validate all metadata.yaml files against the Pydantic schema."""

    def test_schema_validates_valid_metadata(self) -> None:
        """A well-formed metadata dict should validate."""
        data = {
            "name": "test_node",
            "version": "1.0.0",
            "description": "A test node",
            "capabilities": {"standalone": True, "side_effect_class": "read_only"},
            "dependencies": ["omnibase_core>=0.39.0"],
            "tags": ["test"],
        }
        schema = MetadataSchema(**data)
        assert schema.name == "test_node"
        assert schema.capabilities.standalone is True

    def test_all_node_metadata_files_valid(self) -> None:
        """Every metadata.yaml in the nodes directory should validate."""
        metadata_files = list(_NODES_DIR.rglob("metadata.yaml"))
        assert len(metadata_files) >= 3, (
            f"Expected at least 3 metadata files, found {len(metadata_files)}"
        )

        for meta_path in metadata_files:
            with meta_path.open() as f:
                data = yaml.safe_load(f)
            schema = MetadataSchema(**data)
            assert schema.name, f"Missing name in {meta_path}"
            assert schema.version, f"Missing version in {meta_path}"

    def test_deprecation_fields_default_to_not_deprecated(self) -> None:
        """Nodes without deprecation fields default to not deprecated."""
        data = {
            "name": "test_node",
            "version": "1.0.0",
            "description": "A test node",
        }
        schema = MetadataSchema(**data)
        assert schema.deprecated is False
        assert schema.deprecated_by is None
        assert schema.deprecated_reason is None

    def test_deprecation_fields_parse_correctly(self) -> None:
        """A node marked deprecated parses all three deprecation fields."""
        data = {
            "name": "node_merge_sweep",
            "version": "1.0.0",
            "description": "Old node",
            "deprecated": True,
            "deprecated_by": "node_pr_lifecycle_orchestrator",
            "deprecated_reason": "Superseded by orchestrator.",
        }
        schema = MetadataSchema(**data)
        assert schema.deprecated is True
        assert schema.deprecated_by == "node_pr_lifecycle_orchestrator"
        assert schema.deprecated_reason == "Superseded by orchestrator."

    def test_node_merge_sweep_is_deprecated(self) -> None:
        """node_merge_sweep metadata.yaml marks node as deprecated."""
        meta_path = _NODES_DIR / "node_merge_sweep" / "metadata.yaml"
        with meta_path.open() as f:
            data = yaml.safe_load(f)
        schema = MetadataSchema(**data)
        assert schema.deprecated is True
        assert schema.deprecated_by == "node_pr_lifecycle_orchestrator"
        assert schema.deprecated_reason

    def test_node_pr_snapshot_effect_is_deprecated(self) -> None:
        """node_pr_snapshot_effect metadata.yaml marks node as deprecated."""
        meta_path = _NODES_DIR / "node_pr_snapshot_effect" / "metadata.yaml"
        with meta_path.open() as f:
            data = yaml.safe_load(f)
        schema = MetadataSchema(**data)
        assert schema.deprecated is True
        assert schema.deprecated_by == "node_pr_lifecycle_orchestrator"
        assert schema.deprecated_reason
