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
import yaml

from omnimarket.projection.discovery import (
    ALLOWED_SCHEMAS,
    _load_projection_api_section,
    _parse_projection_api_section,
    _parse_projection_api_sections,
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

    def test_projection_metadata_columns_are_parsed(self, tmp_path: Path) -> None:
        section = self._valid_section()
        section.update(
            {
                "cursor_column": "projection_cursor",
                "last_event_id_column": "last_event_id",
                "last_ingest_sequence_column": "last_ingest_sequence",
                "freshness_state_column": "freshness_state",
                "degraded_reason_column": "degraded_reason",
                "observed_at_column": "observed_at",
            }
        )
        p = tmp_path / "contract.yaml"
        cfg = _parse_projection_api_section(section, "node_test", p)

        assert cfg is not None
        assert cfg.cursor_column == "projection_cursor"
        assert cfg.last_event_id_column == "last_event_id"
        assert cfg.last_ingest_sequence_column == "last_ingest_sequence"
        assert cfg.freshness_state_column == "freshness_state"
        assert cfg.degraded_reason_column == "degraded_reason"
        assert cfg.observed_at_column == "observed_at"

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

    def test_non_string_columns_rejected(self, tmp_path: Path) -> None:
        section = self._valid_section()
        section["columns"] = ["col_a", 123]
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

    def test_non_string_schema_rejected(self, tmp_path: Path) -> None:
        section = self._valid_section()
        section["schema"] = 123
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

    def test_optional_string_fields_reject_non_strings(self, tmp_path: Path) -> None:
        for field in ("order_by", "freshness_column"):
            section = self._valid_section()
            section[field] = 123
            p = tmp_path / "contract.yaml"
            cfg = _parse_projection_api_section(section, "node_test", p)
            assert cfg is None

    def test_optional_projection_metadata_fields_reject_non_strings(
        self, tmp_path: Path
    ) -> None:
        for field in (
            "cursor_column",
            "last_event_id_column",
            "last_ingest_sequence_column",
            "freshness_state_column",
            "degraded_reason_column",
            "observed_at_column",
        ):
            section = self._valid_section()
            section[field] = 123
            p = tmp_path / "contract.yaml"
            cfg = _parse_projection_api_section(section, "node_test", p)
            assert cfg is None

    def test_limit_must_be_positive_integer(self, tmp_path: Path) -> None:
        for raw_limit in (0, -1, "100", True):
            section = self._valid_section()
            section["limit"] = raw_limit
            p = tmp_path / "contract.yaml"
            cfg = _parse_projection_api_section(section, "node_test", p)
            assert cfg is None


# ---------------------------------------------------------------------------
# build_projection_topic_map
# ---------------------------------------------------------------------------


class TestBuildProjectionTopicMap:
    def test_ab_compare_reducer_contract_exposes_real_llm_metrics_projection(
        self,
    ) -> None:
        topic_map = build_projection_topic_map()
        cfg = topic_map["onex.snapshot.projection.ab-compare.v1"]
        assert cfg.source_contract == "ab_compare_reducer"
        assert cfg.schema_name == "public"
        assert cfg.table == "llm_call_metrics"
        assert cfg.columns == (
            "correlation_id",
            "model_id",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "estimated_cost_usd",
            "latency_ms",
            "usage_source",
            "created_at",
        )
        assert cfg.order_by == "created_at DESC"
        assert cfg.freshness_column == "created_at"
        assert "*" not in cfg.columns

    def test_delegation_contract_exposes_dashboard_projection_topics(self) -> None:
        topic_map = build_projection_topic_map()

        expected_topics = {
            "delegation",
            "onex.snapshot.projection.delegation.decisions.v1",
            "onex.snapshot.projection.delegation.summary.v1",
            "onex.snapshot.projection.delegation.savings.v1",
            "onex.snapshot.projection.delegation.model-routing.v1",
            "onex.snapshot.projection.delegation.quality-gate.v1",
            "onex.snapshot.projection.delegation.token-usage.v1",
        }

        assert expected_topics.issubset(topic_map)
        assert topic_map["delegation"].table == "delegation_events"
        assert (
            topic_map["onex.snapshot.projection.delegation.savings.v1"].table
            == "projection_delegation_savings"
        )
        assert topic_map[
            "onex.snapshot.projection.delegation.model-routing.v1"
        ].json_columns == ("rows", "by_model", "decision_traces")

    def test_overnight_reducer_exposes_readiness_snapshot(self) -> None:
        topic_map = build_projection_topic_map()

        cfg = topic_map["onex.snapshot.projection.overnight.v1"]

        assert cfg.source_contract == "projection_overnight"
        assert cfg.schema_name == "public"
        assert cfg.table == "projection_overnight_readiness"
        assert cfg.columns == (
            "dimensions",
            "overallStatus",
            "lastCheckedAt",
            "latest_projection_updated_at",
        )
        assert cfg.json_columns == ("dimensions",)
        assert cfg.freshness_column == "latest_projection_updated_at"
        assert cfg.limit == 1

    def test_delegation_reducer_subscribes_to_canonical_terminal_events(self) -> None:
        contract_path = (
            Path(__file__).parents[3]
            / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
        )
        contract = yaml.safe_load(contract_path.read_text())
        topics = set(contract["event_bus"]["subscribe_topics"])

        assert "onex.evt.omnibase-infra.delegation-completed.v1" in topics
        assert "onex.evt.omnibase-infra.delegation-failed.v1" in topics

    def test_routing_reducer_exposes_dashboard_snapshot_view(self) -> None:
        topic_map = build_projection_topic_map()

        cfg = topic_map["onex.snapshot.projection.routing-decision.v1"]

        assert cfg.source_contract == "projection_llm_routing"
        assert cfg.schema_name == "public"
        assert cfg.table == "projection_routing_decision"
        assert cfg.columns == (
            "models",
            "intents",
            "task_presets",
            "routing_rules",
            "captured_at",
            "provisioned",
            "latest_projection_updated_at",
        )
        assert cfg.json_columns == (
            "models",
            "intents",
            "task_presets",
            "routing_rules",
        )
        assert cfg.freshness_column == "latest_projection_updated_at"
        assert cfg.limit == 1

    def test_multiple_projection_api_exposures_are_registered(
        self, tmp_path: Path
    ) -> None:
        p = _write_contract(
            tmp_path,
            """
            name: node_multi_projection
            projection_api:
              expose: true
              exposures:
                - topic: "onex.snapshot.projection.one.v1"
                  table: "projection_one"
                  columns: ["projection_cursor", "last_event_id"]
                  cursor_column: "projection_cursor"
                  last_event_id_column: "last_event_id"
                - topic: "onex.snapshot.projection.two.v1"
                  table: "projection_two"
                  columns: ["projection_cursor", "last_event_id"]
                  cursor_column: "projection_cursor"
                  last_event_id_column: "last_event_id"
            """,
        )
        manifest = _make_manifest([_make_contract_stub(p, "node_multi_projection")])
        result = build_projection_topic_map(manifest)

        assert set(result) == {
            "onex.snapshot.projection.one.v1",
            "onex.snapshot.projection.two.v1",
        }
        assert result["onex.snapshot.projection.one.v1"].cursor_column == (
            "projection_cursor"
        )

    def test_parse_projection_api_sections_accepts_legacy_single_section(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "contract.yaml"
        section = {
            "expose": True,
            "topic": "onex.snapshot.projection.legacy.v1",
            "table": "legacy_projection",
            "columns": ["projection_cursor"],
        }

        configs = _parse_projection_api_sections(section, "node_legacy", p)

        assert len(configs) == 1
        assert configs[0].topic == "onex.snapshot.projection.legacy.v1"

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
