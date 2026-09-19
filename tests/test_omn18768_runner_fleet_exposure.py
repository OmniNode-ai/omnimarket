# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18768 AC3 — the runner-fleet exposure, read from the contract itself.

These load the node's real ``contract.yaml`` through the real discovery path,
so a hand-edited contract that no longer parses, or an exposure that quietly
stopped being bus-backed, fails here rather than at serve time on the lab.

`bus_backed: true` from the first commit is a requirement of the epic's plan,
not a preference: landing a DB-read exposure and converting it later is how a
topic ends up on the 48-entry refused side of the catalog with nobody owning
the conversion. It is only safe because its WRITER lands in the same change —
flipping the flag ahead of a producing writer converts an honest error into a
confident zero.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig

NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_runner_fleet"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"
TOPIC = "onex.snapshot.projection.runner-fleet.v1"
EMIT_TOPIC = "onex.evt.omnibase-infra.runner-fleet.v1"

pytestmark = pytest.mark.unit


def _contract() -> dict[str, object]:
    with open(CONTRACT_PATH) as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return loaded


def _exposure() -> ProjectionTableConfig:
    contract = _contract()
    exposures = load_projection_exposures_from_contract(
        contract, str(contract["name"]), CONTRACT_PATH
    )
    assert len(exposures) == 1, f"expected exactly one exposure, got {len(exposures)}"
    return exposures[0]


def test_the_exposure_is_bus_backed_from_the_first_commit() -> None:
    """AC3 — `bus_backed: true`, not a DB-read exposure converted later."""
    exposure = _exposure()
    assert exposure.topic == TOPIC
    assert exposure.bus_backed is True


def test_the_snapshot_key_is_the_runner_name() -> None:
    """AC2 — one row per runner: a new observation REPLACES that runner's row,
    bounding the cache by fleet size rather than by observation frequency."""
    assert _exposure().key_columns == ("runner_name",)


def test_the_exposure_declares_its_cursor_and_freshness_columns() -> None:
    """AC3 / OMN-18043 — a page boundary and a staleness answer, both declared."""
    exposure = _exposure()
    assert exposure.cursor_column == "projection_cursor"
    assert exposure.freshness_column == "observed_at"
    # The monitor cron is */3. Declaring the cadence is what makes a fleet that
    # stopped being observed degrade to stale instead of serving a confident,
    # hours-old "everything is online".
    assert exposure.expected_event_interval_seconds == 180


def test_offline_runners_lead_the_served_page() -> None:
    """A truncated page is only useful when the rows needing attention lead it.

    A lexical `status DESC` would put `online` first by alphabetical accident —
    exactly backwards — which is why the rank is declared.
    """
    rank = _exposure().order_rank
    assert rank is not None
    assert rank.column == "status"
    assert rank.tiers[0] == ("offline",)
    assert rank.tiers[1] == ("busy",)
    assert rank.tiers[2] == ("online",)


def test_every_status_the_schema_admits_has_a_declared_tier() -> None:
    """A status with no tier is refused at serve time, so an enum value added
    without a tier must fail here rather than on the lab."""
    from omnimarket.nodes.node_projection_runner_fleet.models import EnumRunnerStatus

    rank = _exposure().order_rank
    assert rank is not None
    ranked = {value for tier in rank.tiers for value in tier}
    assert {member.value for member in EnumRunnerStatus} == ranked


def test_the_declared_columns_exist_in_the_owned_migration() -> None:
    """A column declared on the exposure and absent from the table is a served
    page that 500s on the lab, discoverable only by reading it."""
    sql = (
        NODE_DIR / "migrations" / "0000_create_runner_fleet_liveness.sql"
    ).read_text()
    for column in _exposure().columns:
        assert column in sql, f"exposure column {column!r} is not in the migration"


def test_the_node_subscribes_to_the_emitters_topic() -> None:
    """The producer half of this ticket lives in omnibase_infra. If either side
    renames the topic, nothing downstream errors — the reducer simply never
    receives anything — so the name is pinned on both sides."""
    contract = _contract()
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    assert event_bus["subscribe_topics"] == [EMIT_TOPIC]


def test_the_write_path_declares_read_write_not_write() -> None:
    """The writer QUERIES this host's materialized runner names before writing,
    to name the ones that disappeared. `access: write` alone is refused
    fail-closed at the runtime read seam."""
    tables = _contract()["db_io"]["db_tables"]  # type: ignore[index]
    assert isinstance(tables, list)
    assert len(tables) == 1
    assert tables[0]["name"] == "runner_fleet_liveness"
    assert tables[0]["access"] == "read_write"
    assert tables[0]["schema"] == "omninode_internal"


def test_the_ordering_key_is_producer_event_time_not_an_ingest_clock() -> None:
    """Two consumers replaying out of order both have "now" as their ingest
    time, so an ingest clock lets a redelivered older observation win."""
    contract = _contract()
    assert contract["db_io"]["dedupe_key"] == ["runner_name"]  # type: ignore[index]
    assert contract["db_io"]["ordering_key"] == "observed_at"  # type: ignore[index]
