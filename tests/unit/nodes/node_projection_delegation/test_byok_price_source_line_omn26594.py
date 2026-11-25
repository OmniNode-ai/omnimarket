# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev projection's BYOK pricing names where each price came from (OMN-26594).

OMN-23413 prices a call on a customer's own key from the catalogue, the model
registry, then models.dev with its read time. The projection kept only the dollar
figure, so no dev log line could show which of those priced a call, and a reading
could not tell the models.dev leg running from the catalogue leg that always wins
first. These tests pin one INFO line per priced call that carries the source and
the read time, and no such line for a call that stays unpriced.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from omnimarket.nodes.node_projection_delegation.handlers import byok_actual_cost

pytestmark = pytest.mark.unit

LOGGER = "omnimarket.nodes.node_projection_delegation.handlers.byok_actual_cost"
READ_AT = "2026-11-24T22:00:00+00:00"


def _priced_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == LOGGER and "BYOK call priced" in r.getMessage()
    ]


def test_a_catalogue_priced_call_logs_the_catalogue_as_its_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    cost = byok_actual_cost.byok_priced_actual_cost(
        backend_id="byok-glm-general",
        model_name="glm-5.3-flash",
        prompt_tokens=231,
        completion_tokens=255,
    )
    assert cost == pytest.approx(0.00016215)
    lines = _priced_lines(caplog)
    assert len(lines) == 1
    assert "backend_id=byok-glm-general" in lines[0]
    assert "model=glm-5.3-flash" in lines[0]
    assert "price_source=byok_provider_backends" in lines[0]
    assert "price_read_at=None" in lines[0]


def test_a_models_dev_priced_call_logs_models_dev_and_its_read_time(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        byok_actual_cost,
        "models_dev_price_with_read_time",
        lambda _provider, _model_id: ((Decimal("1"), Decimal("2")), READ_AT),
    )
    caplog.set_level(logging.INFO, logger=LOGGER)
    cost = byok_actual_cost.byok_priced_actual_cost(
        backend_id="byok-openrouter",
        model_name="vendor/model-no-source-prices",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )
    assert cost == pytest.approx(3.0)
    lines = _priced_lines(caplog)
    assert len(lines) == 1
    assert "price_source=models.dev" in lines[0]
    assert f"price_read_at={READ_AT}" in lines[0]


def test_an_unpriced_call_logs_no_priced_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        byok_actual_cost,
        "models_dev_price_with_read_time",
        lambda _provider, _model_id: None,
    )
    caplog.set_level(logging.INFO, logger=LOGGER)
    cost = byok_actual_cost.byok_priced_actual_cost(
        backend_id="byok-openrouter",
        model_name="vendor/model-no-source-prices",
        prompt_tokens=10,
        completion_tokens=10,
    )
    assert cost is None
    assert _priced_lines(caplog) == []


def test_a_house_key_call_logs_no_priced_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    cost = byok_actual_cost.byok_priced_actual_cost(
        backend_id=None,
        model_name="glm-5.3-flash",
        prompt_tokens=10,
        completion_tokens=10,
    )
    assert cost is None
    assert _priced_lines(caplog) == []
