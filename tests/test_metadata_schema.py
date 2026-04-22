"""Test metadata.yaml schema validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.enums.enum_node_role import EnumNodeRole
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
            "name": "node_merge_sweep_compute",
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

    def test_node_merge_sweep_compute_is_not_deprecated(self) -> None:
        """node_merge_sweep_compute metadata.yaml — active compute node."""
        meta_path = _NODES_DIR / "node_merge_sweep_compute" / "metadata.yaml"
        with meta_path.open() as f:
            data = yaml.safe_load(f)
        schema = MetadataSchema(**data)
        assert schema.deprecated is False
        assert schema.node_role.value == "compute"

    def test_node_pr_snapshot_effect_is_deprecated(self) -> None:
        """node_pr_snapshot_effect metadata.yaml marks node as deprecated."""
        meta_path = _NODES_DIR / "node_pr_snapshot_effect" / "metadata.yaml"
        with meta_path.open() as f:
            data = yaml.safe_load(f)
        schema = MetadataSchema(**data)
        assert schema.deprecated is True
        assert schema.deprecated_by == "node_pr_lifecycle_orchestrator"
        assert schema.deprecated_reason

    def test_pack_and_display_name_default_to_none(self) -> None:
        """Nodes without pack or display_name default to None."""
        data = {
            "name": "test_node",
            "version": "1.0.0",
            "description": "A test node",
        }
        schema = MetadataSchema(**data)
        assert schema.pack is None

    def test_node_role_field_defaults_to_none(self) -> None:
        """Nodes without node_role field default to None."""
        data = {
            "name": "test_node",
            "version": "1.0.0",
            "description": "A test node",
        }
        schema = MetadataSchema(**data)
        assert schema.node_role is None

    def test_display_name_field_defaults_to_none(self) -> None:
        """Nodes without display_name field default to None."""
        data = {
            "name": "test_node",
            "version": "1.0.0",
            "description": "A test node",
        }
        schema = MetadataSchema(**data)
        assert schema.display_name is None

    def test_pack_field_parses_correctly(self) -> None:
        """pack field accepts a string value for domain package grouping."""
        data = {
            "name": "node_ticket_pipeline",
            "version": "1.0.0",
            "description": "A test node",
            "pack": "pr_lifecycle",
        }
        schema = MetadataSchema(**data)
        assert schema.pack == "pr_lifecycle"

    def test_display_name_field_parses_correctly(self) -> None:
        """display_name field accepts a string value for human-friendly names."""
        data = {
            "name": "test_node",
            "version": "1.0.0",
            "description": "A test node",
            "display_name": "PR Lifecycle Orchestrator",
        }
        schema = MetadataSchema(**data)
        assert schema.display_name == "PR Lifecycle Orchestrator"

    def test_pack_and_display_name_parse_together(self) -> None:
        """Both pack and display_name can be set simultaneously."""
        data = {
            "name": "node_pr_lifecycle_orchestrator",
            "version": "1.0.0",
            "description": "Orchestrates PR lifecycle.",
            "pack": "pr_lifecycle",
            "display_name": "PR Lifecycle Orchestrator",
        }
        schema = MetadataSchema(**data)
        assert schema.pack == "pr_lifecycle"
        assert schema.display_name == "PR Lifecycle Orchestrator"

    def test_pack_and_role_parse_correctly(self) -> None:
        """Pack, display_name, and node_role fields parse correctly."""
        data = {
            "name": "node_ticket_pipeline",
            "version": "1.0.0",
            "description": "Pipeline node",
            "pack": "pipeline",
            "display_name": "Ticket Pipeline",
            "node_role": "orchestrator",
        }
        schema = MetadataSchema(**data)
        assert schema.pack == "pipeline"
        assert schema.display_name == "Ticket Pipeline"
        assert schema.node_role == EnumNodeRole.ORCHESTRATOR

    def test_node_role_compute_is_valid_enum_value(self) -> None:
        """EnumNodeRole must contain COMPUTE for pure-result computation nodes."""
        assert EnumNodeRole.COMPUTE == "compute"

    def test_node_role_compute_parses_in_metadata(self) -> None:
        """metadata.yaml with node_role='compute' must parse to EnumNodeRole.COMPUTE."""
        data = {
            "name": "node_similarity_compute",
            "version": "1.0.0",
            "description": "Computes vector similarity scores.",
            "pack": "memory",
            "display_name": "Similarity Compute",
            "node_role": "compute",
        }
        schema = MetadataSchema(**data)
        assert schema.node_role == EnumNodeRole.COMPUTE

    def test_all_nodes_have_pack_field(self) -> None:
        """Every node metadata.yaml must have a pack field set."""
        metadata_files = list(_NODES_DIR.rglob("metadata.yaml"))
        missing_pack = []
        for meta_path in metadata_files:
            with meta_path.open() as f:
                data = yaml.safe_load(f)
            schema = MetadataSchema(**data)
            if schema.pack is None or not str(schema.pack).strip():
                missing_pack.append(meta_path.parent.name)
        assert not missing_pack, f"Nodes missing pack field: {missing_pack}"

    def test_all_nodes_have_node_role_field(self) -> None:
        """Every node metadata.yaml must have a node_role field set."""
        metadata_files = list(_NODES_DIR.rglob("metadata.yaml"))
        missing_role = []
        for meta_path in metadata_files:
            with meta_path.open() as f:
                data = yaml.safe_load(f)
            schema = MetadataSchema(**data)
            if schema.node_role is None or not str(schema.node_role).strip():
                missing_role.append(meta_path.parent.name)
        assert not missing_role, f"Nodes missing node_role field: {missing_role}"

    def test_all_nodes_have_display_name_field(self) -> None:
        """Every node metadata.yaml must have a display_name field set."""
        metadata_files = list(_NODES_DIR.rglob("metadata.yaml"))
        missing_display = []
        for meta_path in metadata_files:
            with meta_path.open() as f:
                data = yaml.safe_load(f)
            schema = MetadataSchema(**data)
            if schema.display_name is None or not str(schema.display_name).strip():
                missing_display.append(meta_path.parent.name)
        assert not missing_display, (
            f"Nodes missing display_name field: {missing_display}"
        )

    def test_entry_flags_defaults_to_none(self) -> None:
        """Nodes without entry_flags default to None."""
        data = {
            "name": "test_node",
            "version": "1.0.0",
            "description": "A test node",
        }
        schema = MetadataSchema(**data)
        assert schema.entry_flags is None

    def test_entry_flags_parses_dict(self) -> None:
        """An orchestrator node with entry_flags parses them as dict[str, str]."""
        data = {
            "name": "node_ticket_pipeline",
            "version": "1.0.0",
            "description": "Orchestrator node",
            "entry_flags": {
                "dry_run": "Run without making changes",
                "fix_only": "Only apply fixes, skip reporting",
                "inventory_only": "Only collect inventory, skip execution",
            },
        }
        schema = MetadataSchema(**data)
        assert schema.entry_flags is not None
        assert schema.entry_flags["dry_run"] == "Run without making changes"
        assert schema.entry_flags["fix_only"] == "Only apply fixes, skip reporting"
        assert len(schema.entry_flags) == 3

    def test_entry_flags_empty_dict_accepted(self) -> None:
        """An empty entry_flags dict is valid."""
        data = {
            "name": "test_node",
            "version": "1.0.0",
            "description": "A test node",
            "entry_flags": {},
        }
        schema = MetadataSchema(**data)
        assert schema.entry_flags == {}

    def test_existing_metadata_files_parse_without_entry_flags(self) -> None:
        """Metadata files lacking entry_flags parse cleanly and default to None."""
        metadata_files = list(_NODES_DIR.rglob("metadata.yaml"))
        assert len(metadata_files) >= 3

        for meta_path in metadata_files:
            with meta_path.open() as f:
                data = yaml.safe_load(f)
            schema = MetadataSchema(**data)
            if "entry_flags" not in data:
                assert schema.entry_flags is None, (
                    f"{meta_path} unexpectedly has entry_flags set"
                )

    def test_existing_metadata_files_backward_compatible(self) -> None:
        """All existing metadata.yaml files parse without error (backward compat)."""
        metadata_files = list(_NODES_DIR.rglob("metadata.yaml"))
        for meta_path in metadata_files:
            with meta_path.open() as f:
                data = yaml.safe_load(f)
            schema = MetadataSchema(**data)
            assert schema.pack is None or isinstance(schema.pack, str)
            assert schema.display_name is None or isinstance(schema.display_name, str)
