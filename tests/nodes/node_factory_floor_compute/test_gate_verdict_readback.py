# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the later W0 verdict and its named cause instead of the old subject=0 row."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    HandlerFactoryFloor,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import parse_ledger_text
from tests.nodes.node_factory_floor_compute.support import request, wave_plan

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("subject", ["0", "G0", "W0"])
@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_latest_gate_alias_wins_and_exposes_the_recorded_cause(
    subject: str, verdict: str
) -> None:
    # The October 31 row is followed by the November 2 reevaluation. The latter
    # used subject=W0 and named process-defect FRICTION without a merged fix.
    reason = "first failing check is 5 (process-defect FRICTION without a merged fix)"
    rows = parse_ledger_text(
        "L1 2026-10-31T08:18:51Z | STATUS | lane=retro-track-b | state=gate | subject=0 | verdict=fail | every task not merged\n"
        f"L2 2026-11-02T14:01:50Z | STATUS | lane=g0-reeval | state=gate | subject={subject} | verdict={verdict} | {reason}\n"
    )
    facts = request(plans=(wave_plan(),), ledger=rows).model_copy(
        update={"generated_at": datetime(2026, 11, 5, 1, 40, tzinfo=UTC)}
    )
    line = HandlerFactoryFloor().handle(facts).lines[0]
    gate = next(item for st in line.stations for item in st.items if item.id == "G0")

    assert gate.state == ("done" if verdict == "pass" else "stop")
    assert "2026-11-02" in gate.note
    if verdict == "fail":
        assert reason in gate.note
        assert line.blocker is not None
        assert reason in line.blocker.reason
        assert "2026-11-02" in line.blocker.reason
    else:
        assert line.gates.failed == 0
        assert line.blocker is None


def test_gate_alias_is_declared_configuration() -> None:
    # An overlay can change the spelling; W0 is not hardcoded in the handler.
    facts = request()
    config = facts.config.model_dump()
    config["gate_wave_subject_template"] = "wave-{wave}"
    typed_config = type(facts.config).model_validate(config)
    rows = parse_ledger_text(
        "L1 2026-10-31T11:00:00Z | STATUS | state=gate | subject=wave-0 | verdict=pass | passed\n"
    )
    line = (
        HandlerFactoryFloor()
        .handle(facts.model_copy(update={"config": typed_config, "ledger": rows}))
        .lines[0]
    )
    assert line.gates.passed == 1
