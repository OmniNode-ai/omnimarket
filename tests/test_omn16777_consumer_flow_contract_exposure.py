# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16777 — the contract must actually load, and say what it claims.

A projection whose exposure silently fails to parse is excluded at startup and
serves nothing, which on this surface would mean the observability node itself
becomes the next thing that is quietly dead. These assertions are cheap and the
failure they prevent is expensive.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.projection.discovery import load_projection_exposures_from_contract

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_consumer_flow"
    / "contract.yaml"
)


def _contract() -> dict[str, object]:
    with open(_CONTRACT_PATH) as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.unit
def test_exposure_loads_and_is_bus_backed_with_its_full_key() -> None:
    """``bus_backed`` with an empty ``key_columns`` is silently excluded by the
    loader, so the flip has to be asserted together with the key."""
    exposures = load_projection_exposures_from_contract(
        _contract(), "projection_consumer_flow", _CONTRACT_PATH
    )
    assert exposures, "the exposure failed to parse and would serve nothing"
    exposure = exposures[0]
    assert exposure.bus_backed is True
    assert exposure.key_columns == ("consumer_group", "topic", "window_start")


@pytest.mark.unit
def test_db_io_declares_read_access_on_both_tables() -> None:
    """OMN-16690's lesson, applied before it can bite.

    This handler QUERIES both tables before writing (upstream lookup, sequence
    gap detection, stale-write guard). A contract declaring ``access: write`` is
    refused fail-closed at the runtime read seam, which quarantines every event
    while the caller still sees a 202 — the failure looks like success.
    """
    tables = _contract()["db_io"]["db_tables"]  # type: ignore[index]
    assert {table["name"] for table in tables} == {  # type: ignore[index]
        "consumer_flow_windows",
        "topic_produce_windows",
    }
    for table in tables:  # type: ignore[union-attr]
        assert table["access"] == "read_write", (  # type: ignore[index]
            f"{table['name']!r} declares {table['access']!r}; this handler reads "  # type: ignore[index]
            "before it writes, and a write-only declaration is refused at the "
            "runtime read seam"
        )


@pytest.mark.unit
def test_dedupe_and_ordering_keys_are_declared_not_assumed() -> None:
    """Idempotency and ordering are contract facts here, not handler folklore."""
    db_io = _contract()["db_io"]
    assert db_io["dedupe_key"] == [  # type: ignore[index]
        "consumer_group",
        "topic",
        "window_start",
    ]
    assert db_io["ordering_key"] == "ingest_sequence"  # type: ignore[index]


@pytest.mark.unit
def test_the_node_rides_the_existing_heartbeat_and_adds_no_transport() -> None:
    """The carrier is load-bearing: a separate poller would keep reporting on a
    runtime that is already dead."""
    event_bus = _contract()["event_bus"]
    assert event_bus["subscribe_topics"] == [  # type: ignore[index]
        "onex.evt.platform.node-heartbeat.v1"
    ]
    assert event_bus["dlq_topics"], (  # type: ignore[index]
        "a node built to make silent loss visible must not become a new "
        "silent-loss site itself"
    )
