# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing canary refuses to report WORKING from a backend that is not operational."""

from __future__ import annotations

import pytest

from omnimarket.models.model_reading import (
    EnumReadingOutcome,
    EnumReadingVerdict,
)
from omnimarket.nodes.node_landing_canary_effect.models.model_landing_canary import (
    EnumCanaryEnding,
    ModelLandingCanaryCommand,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.unit.nodes.node_landing_canary_effect.canary_support import (
    World,
    operational_overlay,
)

pytestmark = pytest.mark.unit

COMMAND = ModelLandingCanaryCommand(correlation_id="backend-refusal", write=True)


def test_an_in_memory_backend_run_that_lands_inside_the_bar_is_unknown_not_working() -> (
    None
):
    world, ledger = World(), InMemoryLedger()
    result = world.handler(ledger).handle(COMMAND)
    assert result.verdict is EnumReadingVerdict.UNKNOWN
    assert result.outcome is EnumReadingOutcome.READ
    assert result.ending is EnumCanaryEnding.MERGED
    row = ledger.lines[0]
    assert "verdict=UNKNOWN" in row
    assert "backend=inmemory" in row
    assert "tag=drill" in row
    assert "backend-inmemory-is-a-fixture-not-a-landing-reading" in row
    assert "verdict=WORKING" not in row


def test_the_same_run_on_an_operational_backend_still_reads_working() -> None:
    world, ledger = World(), InMemoryLedger()
    result = world.handler(ledger, operational_overlay()).handle(COMMAND)
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.outcome is EnumReadingOutcome.READ
    assert result.ending is EnumCanaryEnding.MERGED
    row = ledger.lines[0]
    assert "verdict=WORKING" in row
    assert "backend=scm" in row


def test_a_stalled_in_memory_run_is_still_not_working() -> None:
    world, ledger = World(), InMemoryLedger()
    world.break_landing_tick_disabled()
    result = world.handler(ledger).handle(COMMAND)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert "verdict=NOT_WORKING" in ledger.lines[0]
