# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20717 AC2: the delegate-skill metrics say whether the cost was measured.

The delegation row stores NULL for an unpriced call only if the metrics can say
the call was not priced. Failure modes: a ladder whose rungs reported no
measured source still claims a measured cost (an unpriced call stored as 0); a
ladder with one measured rung is called unpriced (a priced or free call
nulled); a producer that reports no source at all is called unpriced.
"""

from __future__ import annotations

import pytest

from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _as_usage_source,
    _cost_measured,
)

pytestmark = pytest.mark.unit


def _rung(source: EnumUsageSource | None) -> ModelDelegateSkillAttemptRecord:
    return ModelDelegateSkillAttemptRecord(
        tier="cheap_cloud",
        backend_id="b",
        model_id="m",
        quality_gate_passed=True,
        usage_source=source,
    )


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        ([EnumUsageSource.UNKNOWN], False),
        ([EnumUsageSource.ESTIMATED, EnumUsageSource.UNKNOWN], False),
        ([EnumUsageSource.UNKNOWN, EnumUsageSource.MEASURED], True),
        ([EnumUsageSource.MEASURED], True),
        ([None, None], None),
        ([], None),
        ([None, EnumUsageSource.UNKNOWN], False),
    ],
)
def test_cost_measured_follows_the_rungs_sources(
    sources: list[EnumUsageSource | None], expected: bool | None
) -> None:
    assert _cost_measured([_rung(s) for s in sources]) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("measured", EnumUsageSource.MEASURED),
        ("api", EnumUsageSource.MEASURED),
        ("not-a-source", EnumUsageSource.UNKNOWN),
    ],
)
def test_a_reported_source_is_parsed_and_an_unknown_one_is_not_measured(
    raw: object, expected: EnumUsageSource | None
) -> None:
    assert _as_usage_source(raw) is expected
