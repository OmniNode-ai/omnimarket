"""Unit tests for ProjectionTopicDiscovery (OMN-10490).

Tests:
- expose: true required; expose: false is excluded
- db_io-only contracts are not exposed
- missing topic, table, or columns fields → contract excluded with logged error
- columns: ["*"] is valid
- absent order_by → order_by = None (not "updated_at DESC")
- absent freshness_column → freshness_column = None
- schema whitelist enforced
- no topic derivation from directory name
- no information_schema calls anywhere in discovery module
- _read_db_io_tables is never imported or called
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from omnimarket.projection.discovery import (
    ALLOWED_SCHEMAS,
    _load_projection_api_section,
    _parse_projection_api_section,
    build_projection_topic_map,
)
from omnimarket.projection.models import ProjectionStatus

# ---------------------------------------------------------------------------
# Helpers — build minimal ModelAutoWiringManifest stubs for testing
# ---------------------------------------------------------------------------


def _make_manifest(contracts: list[MagicMock]) -> MagicMock:
    """Stub ModelAutoWiringManifest with given contract list."""
    m = MagicMock()
    m.contracts = contracts
    return m


def _make_contract_stub(contract_path: Path, name: str = "node_test") -> MagicMock:
    """Stub ModelDiscoveredContract pointing at a real contract path."""
    stub = MagicMock()
    stub.contract_path = contract_path
    stub.name = name
    return stub


def _write_contract(tmp_path: Path, content: str) -> Path:
    """Write a contract.yaml under tmp_path and return the path."""
    p = tmp_path / "contract.yaml"
    p.write_text(textwrap.dedent(content))
    return p


# ---------------------------------------------------------------------------
# _load_projection_api_section
# ---------------------------------------------------------------------------


class TestLoadProjectionApiSection:
    def test_returns_none_when_section_absent(self, tmp_path: Path) -> None:
        p = _write_contract(tmp_path, "name: node_x\nnode_type: COMPUTE\n")
        assert _load_projection_api_section(p) is None

    def test_returns_section_when_present(self, tmp_path: Path) -> None:
        p = _write_contract(
            tmp_path,
            """
            name: node_x
            projection_api:
              expose: true
              topic: "t.v1"
              table: "my_table"
              columns: ["col_a"]
            """,
        )
        section = _load_projection_api_section(p)
        assert section is not None
        assert section["topic"] == "t.v1"

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        result = _load_projection_api_section(tmp_path / "nonexistent.yaml")
        assert result is None

    def test_returns_none_for_non_dict_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "contract.yaml"
        p.write_text("- item1\n- item2\n")
        assert _load_projection_api_section(p) is None


# ---------------------------------------------------------------------------
# _parse_projection_api_section
# ---------------------------------------------------------------------------


class TestParseProjectionApiSection:
    def _valid_section(self) -> dict:
        return {
            "expose": True,
            "topic": "onex.snapshot.projection.test.v1",
            "table": "test_table",
            "columns": ["col_a", "col_b"],
            "order_by": "col_a DESC",
            "freshness_column": "col_a",
            "limit": 50,
        }

    def test_parses_valid_section(self, tmp_path: Path) -> None:
        p = tmp_path / "contract.yaml"
        cfg = _parse_projection_api_section(self._valid_section(), "node_test", p)
        assert cfg is not None
        assert cfg.topic == "onex.snapshot.projection.test.v1"
        assert cfg.table == "test_table"
        assert cfg.columns == ("col_a", "col_b")
        assert cfg.order_by == "col_a DESC"
        assert cfg.freshness_column == "col_a"
        assert cfg.limit == 50
        assert cfg.source_contract == "node_test"
        assert cfg.status == ProjectionStatus.OK

    def test_explicit_topic_required(self, tmp_path: Path) -> None:
        section = self._valid_section()
        del section["topic"]
        p = tmp_path / "contract.yaml"
        cfg = _parse_projection_api_section(section, "node_test", p)
        assert cfg is None

    def test_explicit_table_required(self, tmp_path: Path) -> None:
        section = self._valid_section()
        del section["table"]
        p = tmp_path / "contract.yaml"
        cfg = _parse_projection_api_section(section, "node_test", p)
        assert cfg is None

    def test_explicit_columns_required(self, tmp_path: Path) -> None:
        section = self._valid_section()
        del section["columns"]
        p = tmp_path / "contract.yaml"
        cfg = _parse_projection_api_section(section, "node_test", p)
        assert cfg is None

    def test_empty_columns_rejected(self, tmp_path: Path) -> None:
        section = self._valid_section()
        section["columns"] = []
        p = tmp_path / "contract.yaml"
        cfg = _parse_projection_api_section(section, "node_test", p)
        assert cfg is None

    def test_wildcard_columns_valid(self, tmp_path: Path) -> None:
        section = self._valid_section()
        section["columns"] = ["*"]
        p = tmp_path / "contract.yaml"
        cfg = _parse_projection_api_section(section, "node_test", p)
        assert cfg is not None
        assert cfg.columns == ("*",)

    def test_absent_order_by_yields_none_not_updated_at(self, tmp_path: Path) -> None:
        """Absent order_by must produce order_by=None, never default to updated_at."""
        section = self._valid_section()
        del section["order_by"]
        p = tmp_path / "contract.yaml"
        cfg = _parse_projection_api_section(section, "node_test", p)
        assert cfg is not None
        assert cfg.order_by is None
        # Explicitly verify it was not silently defaulted to updated_at
        assert cfg.order_by != "updated_at DESC"

    def test_absent_freshness_column_yields_none(self, tmp_path: Path) -> None:
        """Absent freshness_column must produce freshness_column=None."""
        section = self._valid_section()
        del section["freshness_column"]
        p = tmp_path / "contract.yaml"
        cfg = _parse_projection_api_section(section, "node_test", p)
        assert cfg is not None
        assert cfg.freshness_column is None

    def test_schema_whitelist_enforced(self, tmp_path: Path) -> None:
        """A non-whitelisted schema must cause the contract to be excluded."""
        section = self._valid_section()
        section["schema"] = "private_schema"
        p = tmp_path / "contract.yaml"
        cfg = _parse_projection_api_section(section, "node_test", p)
        assert cfg is None

    def test_allowed_schemas_accepted(self, tmp_path: Path) -> None:
        for schema in ALLOWED_SCHEMAS:
            section = self._valid_section()
            section["schema"] = schema
            p = tmp_path / "contract.yaml"
            cfg = _parse_projection_api_section(section, "node_test", p)
            assert cfg is not None, f"Schema {schema!r} should be allowed"
            assert cfg.schema_name == schema

    def test_default_schema_is_public(self, tmp_path: Path) -> None:
        section = self._valid_section()
        # no "schema" key
        p = tmp_path / "contract.yaml"
        cfg = _parse_projection_api_section(section, "node_test", p)
        assert cfg is not None
        assert cfg.schema_name == "public"

    def test_default_limit_is_100(self, tmp_path: Path) -> None:
        section = self._valid_section()
        del section["limit"]
        p = tmp_path / "contract.yaml"
        cfg = _parse_projection_api_section(section, "node_test", p)
        assert cfg is not None
        assert cfg.limit == 100


# ---------------------------------------------------------------------------
# build_projection_topic_map
# ---------------------------------------------------------------------------


class TestBuildProjectionTopicMap:
    def test_expose_true_required(self, tmp_path: Path) -> None:
        """Contracts with projection_api.expose: false are excluded."""
        p = _write_contract(
            tmp_path,
            """
            name: node_x
            projection_api:
              expose: false
              topic: "t.v1"
              table: "my_table"
              columns: ["col_a"]
            """,
        )
        manifest = _make_manifest([_make_contract_stub(p, "node_x")])
        result = build_projection_topic_map(manifest)
        assert len(result) == 0

    def test_expose_absent_is_excluded(self, tmp_path: Path) -> None:
        """Contracts with projection_api section but no expose field are excluded."""
        p = _write_contract(
            tmp_path,
            """
            name: node_x
            projection_api:
              topic: "t.v1"
              table: "my_table"
              columns: ["col_a"]
            """,
        )
        manifest = _make_manifest([_make_contract_stub(p, "node_x")])
        result = build_projection_topic_map(manifest)
        assert len(result) == 0

    def test_db_io_only_not_exposed(self, tmp_path: Path) -> None:
        """Contracts with db_io.db_tables but no projection_api are excluded."""
        p = _write_contract(
            tmp_path,
            """
            name: node_x
            db_io:
              db_tables:
                - name: some_table
                  access: write
            """,
        )
        manifest = _make_manifest([_make_contract_stub(p, "node_x")])
        result = build_projection_topic_map(manifest)
        assert len(result) == 0

    def test_valid_contract_registered(self, tmp_path: Path) -> None:
        """A valid expose: true contract is registered with the declared topic."""
        p = _write_contract(
            tmp_path,
            """
            name: node_x
            projection_api:
              expose: true
              topic: "onex.snapshot.projection.test.v1"
              table: "test_table"
              columns: ["col_a", "col_b"]
              order_by: "col_a DESC"
              freshness_column: "col_a"
              limit: 100
            """,
        )
        manifest = _make_manifest([_make_contract_stub(p, "node_x")])
        result = build_projection_topic_map(manifest)
        assert "onex.snapshot.projection.test.v1" in result
        cfg = result["onex.snapshot.projection.test.v1"]
        assert cfg.table == "test_table"
        assert cfg.source_contract == "node_x"
        assert cfg.status == ProjectionStatus.OK

    def test_no_topic_derivation_from_directory(self, tmp_path: Path) -> None:
        """Topic name is taken from contract, never derived from directory name."""
        node_dir = tmp_path / "node_my_special_projection"
        node_dir.mkdir()
        p = node_dir / "contract.yaml"
        p.write_text(
            textwrap.dedent(
                """
                name: node_my_special_projection
                projection_api:
                  expose: true
                  topic: "onex.snapshot.projection.explicit-name.v1"
                  table: "test_table"
                  columns: ["col_a"]
                """
            )
        )
        stub = _make_contract_stub(p, "node_my_special_projection")
        manifest = _make_manifest([stub])
        result = build_projection_topic_map(manifest)
        # The topic must be the explicitly declared one — not derived from dir name.
        assert "onex.snapshot.projection.explicit-name.v1" in result
        # The directory name as a topic must NOT appear.
        assert "node_my_special_projection" not in result

    def test_missing_topic_field_excludes_contract(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        p = _write_contract(
            tmp_path,
            """
            name: node_x
            projection_api:
              expose: true
              table: "test_table"
              columns: ["col_a"]
            """,
        )
        manifest = _make_manifest([_make_contract_stub(p, "node_x")])
        with caplog.at_level(logging.ERROR, logger="omnimarket.projection.discovery"):
            result = build_projection_topic_map(manifest)
        assert len(result) == 0
        assert any("topic" in msg for msg in caplog.messages)

    def test_missing_columns_field_excludes_contract(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        p = _write_contract(
            tmp_path,
            """
            name: node_x
            projection_api:
              expose: true
              topic: "t.v1"
              table: "test_table"
            """,
        )
        manifest = _make_manifest([_make_contract_stub(p, "node_x")])
        with caplog.at_level(logging.ERROR, logger="omnimarket.projection.discovery"):
            result = build_projection_topic_map(manifest)
        assert len(result) == 0
        assert any("columns" in msg for msg in caplog.messages)

    def test_missing_table_field_excludes_contract(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        p = _write_contract(
            tmp_path,
            """
            name: node_x
            projection_api:
              expose: true
              topic: "t.v1"
              columns: ["col_a"]
            """,
        )
        manifest = _make_manifest([_make_contract_stub(p, "node_x")])
        with caplog.at_level(logging.ERROR, logger="omnimarket.projection.discovery"):
            result = build_projection_topic_map(manifest)
        assert len(result) == 0
        assert any("table" in msg for msg in caplog.messages)

    def test_schema_whitelist_enforced_at_build(self, tmp_path: Path) -> None:
        p = _write_contract(
            tmp_path,
            """
            name: node_x
            projection_api:
              expose: true
              topic: "t.v1"
              table: "test_table"
              schema: "secret_internal"
              columns: ["col_a"]
            """,
        )
        manifest = _make_manifest([_make_contract_stub(p, "node_x")])
        result = build_projection_topic_map(manifest)
        assert len(result) == 0

    def test_duplicate_topic_second_ignored(self, tmp_path: Path) -> None:
        """If two contracts declare the same topic, the first wins."""
        p1 = tmp_path / "c1.yaml"
        p1.write_text(
            textwrap.dedent(
                """
                name: node_a
                projection_api:
                  expose: true
                  topic: "shared.topic.v1"
                  table: "table_a"
                  columns: ["col_a"]
                """
            )
        )
        p2 = tmp_path / "c2.yaml"
        p2.write_text(
            textwrap.dedent(
                """
                name: node_b
                projection_api:
                  expose: true
                  topic: "shared.topic.v1"
                  table: "table_b"
                  columns: ["col_b"]
                """
            )
        )
        stubs = [
            _make_contract_stub(p1, "node_a"),
            _make_contract_stub(p2, "node_b"),
        ]
        manifest = _make_manifest(stubs)
        result = build_projection_topic_map(manifest)
        assert len(result) == 1
        assert result["shared.topic.v1"].source_contract == "node_a"

    def test_no_column_introspection(self) -> None:
        """Discovery never touches information_schema."""
        import omnimarket.projection.discovery as disc_module

        source = Path(disc_module.__file__).read_text()
        assert "information_schema" not in source, (
            "discovery.py must not reference information_schema"
        )

    def test_private_function_not_called(self) -> None:
        """_read_db_io_tables from omnibase_infra is never imported or called."""
        import omnimarket.projection.discovery as disc_module

        source = Path(disc_module.__file__).read_text()
        assert "_read_db_io_tables" not in source, (
            "discovery.py must not call _read_db_io_tables (private API)"
        )

    def test_no_topic_derivation_from_name_field(self, tmp_path: Path) -> None:
        """Topic is always the projection_api.topic field, never the contract name."""
        p = _write_contract(
            tmp_path,
            """
            name: this_is_not_the_topic
            projection_api:
              expose: true
              topic: "the.real.topic.v1"
              table: "test_table"
              columns: ["col_a"]
            """,
        )
        manifest = _make_manifest([_make_contract_stub(p, "this_is_not_the_topic")])
        result = build_projection_topic_map(manifest)
        assert "the.real.topic.v1" in result
        assert "this_is_not_the_topic" not in result

    def test_calls_discover_contracts_when_no_manifest(self) -> None:
        """When manifest=None, discover_contracts() is called exactly once."""
        fake_manifest = _make_manifest([])
        with patch(
            "omnimarket.projection.discovery.discover_contracts",
            return_value=fake_manifest,
        ) as mock_discover:
            build_projection_topic_map(manifest=None)
        mock_discover.assert_called_once_with()
