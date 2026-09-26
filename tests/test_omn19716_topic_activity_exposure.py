# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19716 projection exposure contract tests."""

from pathlib import Path

import pytest
import yaml

from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig

pytestmark = pytest.mark.unit
NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_topic_activity"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load(CONTRACT_PATH.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _exposure() -> ProjectionTableConfig:
    contract = _contract()
    exposures = load_projection_exposures_from_contract(
        contract, str(contract["name"]), CONTRACT_PATH
    )
    assert len(exposures) == 1
    return exposures[0]


def test_exposure_is_bus_backed_with_cursor_and_freshness() -> None:
    exposure = _exposure()
    assert exposure.topic == "onex.snapshot.projection.topic-activity.v1"
    assert exposure.bus_backed is True
    assert exposure.cursor_column == "projection_cursor"
    assert exposure.freshness_column == "sampled_at"
    assert exposure.expected_event_interval_seconds == 60
    assert exposure.key_columns == ("topic",)


def test_most_active_topics_are_ordered_first() -> None:
    exposure = _exposure()
    assert exposure.order_by_spec == (
        ("rate_last_hour_per_second", "DESC", "LAST"),
        ("topic", "ASC", None),
    )


def test_all_exposed_columns_exist_in_the_owned_migration() -> None:
    sql = (NODE_DIR / "migrations" / "0000_create_topic_activity.sql").read_text()
    for column in _exposure().columns:
        assert column in sql


def test_exposure_does_not_opt_out_of_reader_coverage() -> None:
    projection = _contract()["projection_api"]
    assert "consumers" not in projection  # type: ignore[operator]
    assert "consumers_reason" not in projection  # type: ignore[operator]


def test_runtime_dispatch_resolves_only_the_writer() -> None:
    """OMN-19721 found a routed pure fold dead-lettering every event beside its
    writer; this reducer routes the writer alone."""
    from omnibase_infra.runtime.auto_wiring.discovery import _parse_contract

    contract = _parse_contract(
        contract_path=CONTRACT_PATH,
        entry_point_name="node_projection_topic_activity",
        package_name="omnimarket",
        package_version="test",
    )
    assert contract.handler_routing is not None
    assert [entry.handler.name for entry in contract.handler_routing.handlers] == [
        "TopicActivityProjectionWriter"
    ]
