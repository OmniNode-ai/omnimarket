# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the work-state projection tests: the example overlay as the rules, and a ledger row builder."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.events.work_state_projection import (
    ModelWorkStateRules,
)
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    evidence_row_from_text,
)

NOW = datetime(2026, 11, 7, 13, 0, tzinfo=UTC)
EXAMPLE_OVERLAY = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_work_state_projection_compute/overlay.example.yaml"
)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def rules() -> ModelWorkStateRules:
    return load_structured_overlay(
        "node_work_state_projection_compute",
        ModelWorkStateRules,
        explicit=yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8")),
    )


RowBuilder = Callable[..., ModelLedgerEvidenceRow]


@pytest.fixture
def row() -> RowBuilder:
    """A ledger row `minutes_ago` before NOW: `row("CLAIM", "lane=x", "ticket=T-1", minutes_ago=30)`."""

    def build(
        row_type: str, *cells: str, minutes_ago: float = 0.0
    ) -> ModelLedgerEvidenceRow:
        stamp = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        text = " | ".join((stamp, row_type, *cells, "text"))
        parsed = evidence_row_from_text(text)
        assert parsed is not None, text
        return parsed

    return build
