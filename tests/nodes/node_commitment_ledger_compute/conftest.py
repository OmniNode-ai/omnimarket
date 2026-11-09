# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the commitment tests: the example overlay as the rules and a ledger row builder."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.commitment import (
    ModelCommitmentRules,
)
from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    evidence_row_from_text,
)

NOW = datetime(2026, 11, 7, 16, 0, tzinfo=UTC)
EXAMPLE_OVERLAY = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_commitment_ledger_compute/overlay.example.yaml"
)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def commitment_rules() -> ModelCommitmentRules:
    return load_structured_overlay(
        "node_commitment_ledger_compute",
        ModelCommitmentRules,
        explicit=yaml.safe_load(EXAMPLE_OVERLAY.read_text(encoding="utf-8")),
    )


RowBuilder = Callable[..., ModelLedgerEvidenceRow]


@pytest.fixture
def row() -> RowBuilder:
    """A ledger row `minutes_ago` before NOW: `row("STATUS", "lane=x", "kind=commitment", minutes_ago=30)`."""

    def build(
        row_type: str, *cells: str, minutes_ago: float = 0.0
    ) -> ModelLedgerEvidenceRow:
        stamp = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        text = " | ".join((stamp, row_type, *cells, "text"))
        parsed = evidence_row_from_text(text)
        assert parsed is not None, text
        return parsed

    return build
