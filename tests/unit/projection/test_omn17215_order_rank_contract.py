# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17215 AC4: ``projection_api.order_rank`` is parsed and validated at
contract load, and only the exposure that declares one changes order.

A malformed rank hard-fails startup exactly like a malformed ``order_by``: it is
neither silently dropped (the page reverts to an order that buries STALLED rows)
nor allowed to exclude a served exposure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.projection.discovery import (
    MalformedOrderBySpecError,
    MalformedOrderRankError,
    _parse_projection_api_section,
    build_projection_topic_map,
)
from omnimarket.projection.models import (
    ProjectionOrderRank,
    ProjectionTableConfig,
    UnrankedOrderValueError,
)

_CONSUMER_FLOW_TOPIC = "onex.snapshot.projection.consumer-flow.v1"
_PATH = Path("contract.yaml")


def _section(**overrides: Any) -> dict[str, object]:
    section: dict[str, object] = {
        "expose": True,
        "topic": "onex.snapshot.projection.example.v1",
        "table": "example_rows",
        "schema": "public",
        "columns": ["id", "state", "updated_at"],
        "order_by": "updated_at DESC",
        "cursor_column": "id",
    }
    section.update(overrides)
    return section


def _parse(**overrides: Any) -> ProjectionTableConfig | None:
    return _parse_projection_api_section(_section(**overrides), "node_example", _PATH)


@pytest.mark.unit
def test_absent_order_rank_parses_to_none() -> None:
    cfg = _parse()
    assert cfg is not None
    assert cfg.order_rank is None
    assert cfg.order_by_spec == (("updated_at", "DESC", None),)


@pytest.mark.unit
def test_well_formed_order_rank_parses_into_tiers() -> None:
    cfg = _parse(order_rank={"column": "state", "tiers": [["BAD", "WORSE"], ["OK"]]})
    assert cfg is not None
    assert cfg.order_rank == ProjectionOrderRank(
        column="state", tiers=(("BAD", "WORSE"), ("OK",))
    )
    assert cfg.order_rank.rank_of("WORSE") == 0
    assert cfg.order_rank.rank_of("OK") == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "order_rank",
    [
        pytest.param(["state"], id="not-a-mapping"),
        pytest.param({"column": "state"}, id="missing-tiers"),
        pytest.param({"tiers": [["OK"]]}, id="missing-column"),
        pytest.param(
            {"column": "state", "tiers": [["OK"]], "default": 9}, id="extra-key"
        ),
        pytest.param({"column": "not_declared", "tiers": [["OK"]]}, id="unknown-col"),
        pytest.param({"column": "", "tiers": [["OK"]]}, id="empty-column"),
        pytest.param({"column": "state", "tiers": []}, id="no-tiers"),
        pytest.param({"column": "state", "tiers": [["OK"], []]}, id="empty-tier"),
        pytest.param({"column": "state", "tiers": ["OK"]}, id="tier-not-a-list"),
        pytest.param({"column": "state", "tiers": [["OK", 1]]}, id="non-string"),
        pytest.param({"column": "state", "tiers": [["OK", ""]]}, id="empty-value"),
        pytest.param(
            {"column": "state", "tiers": [["BAD", "OK"], ["OK"]]}, id="duplicate"
        ),
    ],
)
def test_malformed_order_rank_hard_fails_contract_load(order_rank: object) -> None:
    with pytest.raises(MalformedOrderRankError) as excinfo:
        _parse(order_rank=order_rank)
    # Same startup hard-fail class as a malformed order_by.
    assert isinstance(excinfo.value, MalformedOrderBySpecError)
    assert "projection_api.order_rank" in str(excinfo.value)


@pytest.mark.unit
def test_direct_construction_rejects_a_rank_on_an_undeclared_column() -> None:
    with pytest.raises(ValueError, match="order_rank"):
        ProjectionTableConfig(
            topic="t",
            table="t",
            columns=("id",),
            order_rank=ProjectionOrderRank(column="state", tiers=(("OK",),)),
        )


@pytest.mark.unit
def test_unranked_value_raises_rather_than_sorting_last() -> None:
    rank = ProjectionOrderRank(column="state", tiers=(("BAD",), ("OK",)))
    with pytest.raises(UnrankedOrderValueError) as excinfo:
        rank.rank_of("NEW_STATE")
    assert excinfo.value.column == "state"
    assert excinfo.value.value == "NEW_STATE"


@pytest.mark.unit
def test_sql_order_term_is_derived_from_the_declared_tiers() -> None:
    rank = ProjectionOrderRank(column="state", tiers=(("BAD", "O'DD"), ("OK",)))
    assert rank.sql_order_term() == (
        "CASE WHEN state IN ('BAD', 'O''DD') THEN 0 WHEN state IN ('OK') THEN 1 END ASC"
    )


@pytest.mark.unit
def test_only_consumer_flow_declares_an_order_rank_across_shipped_contracts() -> None:
    """Every other shipped exposure keeps order_rank None, so its presented
    order is exactly its order_by, unchanged by this ticket."""
    topic_map = build_projection_topic_map()
    ranked = {topic for topic, cfg in topic_map.items() if cfg.order_rank is not None}
    assert ranked == {_CONSUMER_FLOW_TOPIC}
    assert len(topic_map) > 1
