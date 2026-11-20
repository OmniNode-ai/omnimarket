# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation call prices an uncatalogued customer-key model from the price list
shipped with the install, and never opens a connection to models.dev to do it.

The customer-machine probe grades every external connect() of the keyed call; a
live price fetch inside the call is one it cannot attribute to the provider. These
tests drive the same pricing function the call uses (``_compute_cost``) with the
real pricing index restored (the suite's autouse fixture empties it) and with the
network closed, so a fetch fails the test rather than passing it silently.
"""

from __future__ import annotations

import socket
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.handlers import model_catalog
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    _compute_cost,
)

pytestmark = pytest.mark.unit

REFLECTION = "https://api.reflection.ai/openai/v1/chat/completions"
BEAM = "Beam-1.0-501B-A23B"


@pytest.fixture
def real_pricing_with_closed_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The real pricing index, and any socket connect or fetch recorded and refused."""
    attempts: list[str] = []

    def refuse_connect(self: socket.socket, address: Any) -> None:
        attempts.append(f"connect {address!r}")
        raise OSError("network closed for this test")

    def refuse_fetch(url: str, timeout: float) -> bytes:
        attempts.append(f"fetch {url}")
        raise OSError("fetch refused for this test")

    monkeypatch.setattr(socket.socket, "connect", refuse_connect)
    monkeypatch.setattr(model_catalog, "_fetch", refuse_fetch)
    monkeypatch.setattr(
        model_catalog, "pricing_models_dev_index", model_catalog.shipped_pricing_index
    )
    return attempts


def test_an_uncatalogued_model_is_priced_from_the_shipped_list_with_no_connect(
    real_pricing_with_closed_network: list[str],
) -> None:
    actual, _opus, _savings, cost_basis = _compute_cost(
        BEAM,
        1_000_000,
        1_000_000,
        model_tier="cheap_cloud",
        endpoint_url=REFLECTION,
        provider="byok-reflection",
    )
    assert actual == Decimal("0")
    assert cost_basis is EnumCostBasis.ZERO_MARGINAL_API_COST
    assert real_pricing_with_closed_network == []


def test_a_paid_shipped_price_is_booked_as_cloud_cost(
    real_pricing_with_closed_network: list[str],
) -> None:
    price = model_catalog.models_dev_price_per_1m("openrouter", "qwen/qwen-plus")
    assert price == (Decimal("0.26"), Decimal("0.78"))
    assert real_pricing_with_closed_network == []


def test_a_model_absent_from_the_shipped_list_stays_unpriced_and_never_fetches(
    real_pricing_with_closed_network: list[str],
) -> None:
    """Negative control: an unlisted model is unpriced, and the call still never fetches."""
    assert (
        model_catalog.models_dev_price_per_1m("openrouter", "no-such/model-zzz") is None
    )
    actual, _opus, _savings, cost_basis = _compute_cost(
        "no-such/model-zzz",
        1000,
        1000,
        model_tier="cheap_cloud",
        endpoint_url=REFLECTION,
        provider="byok-reflection",
    )
    assert actual == Decimal("0")
    assert cost_basis is EnumCostBasis.UNKNOWN
    assert real_pricing_with_closed_network == []


def test_the_explorer_refresh_still_fetches_but_the_pricing_path_does_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Positive control: the fetch hook is live, so an empty record above means no fetch."""
    seen: list[str] = []

    def record_fetch(url: str, timeout: float) -> bytes:
        seen.append(url)
        return b"{}"

    monkeypatch.setattr(model_catalog, "_fetch", record_fetch)
    model_catalog.shipped_pricing_index()
    assert seen == []
    model_catalog.load_models_dev(tmp_path)  # a cold home: the explorer refreshes
    assert len(seen) == 1


def test_the_shipped_list_parses_and_prices_every_entry_with_two_numbers() -> None:
    index = model_catalog.shipped_pricing_index()
    assert len(index.models) > 100
    assert index.stale is False
    for (_provider, _model), entry in index.models.items():
        cost = entry["cost"]
        assert cost["input"] >= 0
        assert cost["output"] >= 0
