# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_ledger_stats_compute.

Tests cover:
- Contract YAML structure and topic naming
- Handler pure-compute logic: empty input, pass/fail counts,
  per-model aggregation, ledger hit rate, avg_attempts rounding
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_ledger_stats_compute.handlers.handler_ledger_stats import (
    HandlerLedgerStats,
)
from omnimarket.nodes.node_ledger_stats_compute.models.model_chain_record import (
    ModelChainRecord,
)
from omnimarket.nodes.node_ledger_stats_compute.models.model_ledger_stats_request import (
    ModelLedgerStatsRequest,
)

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_ledger_stats_compute"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_yaml_is_well_formed() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    assert data["name"] == "node_ledger_stats_compute"
    assert data["node_type"] == "compute"
    assert isinstance(data["contract_version"], dict)
    assert data["contract_version"]["major"] == 1


@pytest.mark.unit
def test_contract_declares_expected_topics() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    bus = data["event_bus"]
    assert "onex.cmd.omnimarket.ledger-stats-requested.v1" in bus["subscribe_topics"]
    assert "onex.evt.omnimarket.ledger-stats-completed.v1" in bus["publish_topics"]


@pytest.mark.unit
def test_contract_terminal_event_matches_publish_topic() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    terminal = data["terminal_event"]
    published = data["event_bus"]["publish_topics"]
    assert terminal in published, (
        f"terminal_event '{terminal}' must appear in event_bus.publish_topics"
    )


@pytest.mark.unit
def test_contract_descriptor_is_pure_compute() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    desc = data["descriptor"]
    assert desc["node_archetype"] == "compute"
    assert desc["purity"] == "pure"
    assert desc["idempotent"] is True


@pytest.mark.unit
def test_contract_handler_routing_present() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    routing = data["handler_routing"]
    handlers = routing["handlers"]
    assert len(handlers) >= 1
    entry = handlers[0]
    assert entry["handler"]["name"] == "HandlerLedgerStats"
    assert "node_ledger_stats_compute" in entry["handler"]["module"]


# ---------------------------------------------------------------------------
# Handler logic tests
# ---------------------------------------------------------------------------


def _make_request(*records: ModelChainRecord) -> ModelLedgerStatsRequest:
    return ModelLedgerStatsRequest(chains=tuple(records))


@pytest.mark.unit
def test_empty_input_returns_zero_stats() -> None:
    result = HandlerLedgerStats().handle(_make_request())
    assert result.total_chains == 0
    assert result.pass_count == 0
    assert result.fail_count == 0
    assert result.by_model == {}
    assert result.ledger_hit_rate == 0.0


@pytest.mark.unit
def test_single_passing_chain() -> None:
    record = ModelChainRecord(contract_passed=True, model_id="qwen3", attempt_count=1)
    result = HandlerLedgerStats().handle(_make_request(record))
    assert result.total_chains == 1
    assert result.pass_count == 1
    assert result.fail_count == 0
    assert result.ledger_hit_rate == 0.0
    assert result.by_model["qwen3"].passed == 1
    assert result.by_model["qwen3"].failed == 0
    assert result.by_model["qwen3"].avg_attempts == 1.0


@pytest.mark.unit
def test_single_failing_chain() -> None:
    record = ModelChainRecord(contract_passed=False, model_id="glm", attempt_count=3)
    result = HandlerLedgerStats().handle(_make_request(record))
    assert result.total_chains == 1
    assert result.pass_count == 0
    assert result.fail_count == 1
    assert result.by_model["glm"].failed == 1
    assert result.by_model["glm"].avg_attempts == 3.0


@pytest.mark.unit
def test_ledger_hit_via_ledger_hit_flag() -> None:
    records = (
        ModelChainRecord(contract_passed=True, model_id="m1", ledger_hit=True),
        ModelChainRecord(contract_passed=True, model_id="m1", ledger_hit=False),
    )
    result = HandlerLedgerStats().handle(_make_request(*records))
    assert result.ledger_hit_rate == 0.5


@pytest.mark.unit
def test_ledger_hit_via_reference_chains_flag() -> None:
    records = (
        ModelChainRecord(
            contract_passed=True, model_id="m1", has_reference_chains=True
        ),
        ModelChainRecord(contract_passed=False, model_id="m1"),
        ModelChainRecord(contract_passed=True, model_id="m1"),
    )
    result = HandlerLedgerStats().handle(_make_request(*records))
    # Only first record has a reference chain → 1/3
    assert result.ledger_hit_rate == round(1 / 3, 2)


@pytest.mark.unit
def test_multi_model_aggregation() -> None:
    records = (
        ModelChainRecord(contract_passed=True, model_id="alpha", attempt_count=1),
        ModelChainRecord(contract_passed=True, model_id="alpha", attempt_count=3),
        ModelChainRecord(contract_passed=False, model_id="beta", attempt_count=2),
        ModelChainRecord(contract_passed=True, model_id="beta", attempt_count=2),
    )
    result = HandlerLedgerStats().handle(_make_request(*records))
    assert result.total_chains == 4
    assert result.pass_count == 3
    assert result.fail_count == 1

    alpha = result.by_model["alpha"]
    assert alpha.passed == 2
    assert alpha.failed == 0
    assert alpha.avg_attempts == 2.0  # (1+3)/2

    beta = result.by_model["beta"]
    assert beta.passed == 1
    assert beta.failed == 1
    assert beta.avg_attempts == 2.0  # (2+2)/2


@pytest.mark.unit
def test_all_pass_ledger_hit_rate_is_one() -> None:
    records = tuple(
        ModelChainRecord(contract_passed=True, model_id="m", ledger_hit=True)
        for _ in range(5)
    )
    result = HandlerLedgerStats().handle(_make_request(*records))
    assert result.ledger_hit_rate == 1.0
    assert result.pass_count == 5
    assert result.fail_count == 0


@pytest.mark.unit
def test_unknown_model_id_default() -> None:
    # When model_id is not supplied it defaults to "unknown"
    record = ModelChainRecord(contract_passed=True)
    result = HandlerLedgerStats().handle(_make_request(record))
    assert "unknown" in result.by_model


@pytest.mark.unit
def test_avg_attempts_rounds_to_two_decimal_places() -> None:
    # 3 attempts over 3 records: avg = 1.0, 2.0, 3.0 → combined (1+2+3)/3 = 2.0
    records = (
        ModelChainRecord(contract_passed=True, model_id="m", attempt_count=1),
        ModelChainRecord(contract_passed=True, model_id="m", attempt_count=2),
        ModelChainRecord(contract_passed=True, model_id="m", attempt_count=3),
    )
    result = HandlerLedgerStats().handle(_make_request(*records))
    assert result.by_model["m"].avg_attempts == 2.0


@pytest.mark.unit
def test_result_is_immutable() -> None:
    record = ModelChainRecord(contract_passed=True)
    result = HandlerLedgerStats().handle(_make_request(record))
    import pydantic

    with pytest.raises((pydantic.ValidationError, TypeError)):
        result.total_chains = 999  # type: ignore[misc]
