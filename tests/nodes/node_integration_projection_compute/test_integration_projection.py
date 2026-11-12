# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The integration projection (OMN-23899 AC1): golden, error chains and named breaks.

The batch is four lines of the orchestrator's integration ledger of 2026-11-12, typed by the pass's own judgment.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.integration_reading import (
    EnumIntegrationGapClass,
    ModelIntegrationReading,
    ModelIntegrationReadingBatch,
)
from omnimarket.nodes.node_integration_projection_compute.handlers import (
    HandlerIntegrationProjection,
)

pytestmark = pytest.mark.unit

READ_AT = "2026-11-12T01:25:00Z"


def reading(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "a change",
        "target": "the dev runtime",
        "read_at": READ_AT,
        "origin": "integration-pass:orchestrator-31d9",
    }
    return {**base, **fields}


READINGS: list[dict[str, Any]] = [
    reading(
        repository="overlays",
        number=301,
        title="text steps ordered local first",
        merged_at="2026-11-11T21:17:00Z",
        state="not_running",
        gap_class="needs_proof",
        gap="the local rung answered from a free hosted model; no dev-runtime receipt yet",
        step="one text step through the dev lane answered by a GPU",
        owner="lane local-prove-5270",
    ),
    reading(
        repository="omnimarket",
        number=5843,
        title="delegation scenario R8 reads tenant totals over the bus",
        merged_at="2026-11-11T22:44:00Z",
        state="blocked",
        gap_class="waits_on_merge",
        gap="the two tenant read nodes are not registered, so no topics",
        step="omnimarket 5874 merges, runtime rebuilds, run R8",
        owner="Mac job r8-finish-0030",
        companions=["overlays#289"],
    ),
    reading(
        repository="omnimarket",
        number=5856,
        title="lane guard writes typed end records",
        merged_at="2026-11-11T23:45:00Z",
        state="defect",
        gap_class="needs_fix",
        gap="lanes run the plugin's copy of the guard; this fixed omnimarket's copy",
        step="one copy, overlay rows, plugin bump",
        owner="",
    ),
    reading(
        repository="omnimarket",
        number=5749,
        title="pins for two repaired shared checks",
        merged_at="2026-11-11T23:50:00Z",
        target="CI",
        state="ci_only",
        gap_class="none",
    ),
    reading(
        repository="omnibase_infra",
        number=5896,
        title="runtime grants on the four merge alarm tables",
        merged_at="2026-11-11T21:31:00Z",
        state="running",
        gap_class="none",
        read_at="2026-11-11T22:36:00Z",
    ),
]

BATCH = {
    "as_of": READ_AT,
    "origin": "integration-pass:orchestrator-31d9",
    "readings": READINGS,
}

GOLDEN_COUNTS = {
    "merged": 5,
    "seen_running": 1,
    "settled_elsewhere": 1,
    "unintegrated": 3,
    "by_gap_class": {
        "needs_fix": 1,
        "waits_on_merge": 1,
        "needs_install": 0,
        "needs_proof": 1,
        "needs_reading": 0,
    },
    "oldest_unintegrated": {"key": "overlays#301", "merged_at": "2026-11-11T21:17:00Z"},
}
GOLDEN_GROUPS = [
    {"gap_class": "needs_fix", "keys": ["omnimarket#5856"]},
    {"gap_class": "waits_on_merge", "keys": ["omnimarket#5843"]},
    {"gap_class": "needs_proof", "keys": ["overlays#301"]},
]
GOLDEN_ROW_ORDER = [
    "overlays#301",
    "omnibase_infra#5896",
    "omnimarket#5843",
    "omnimarket#5856",
    "omnimarket#5749",
]


def project(batch: dict[str, Any]) -> dict[str, Any]:
    result = HandlerIntegrationProjection().handle(
        ModelIntegrationReadingBatch.model_validate(batch)
    )
    return result.model_dump(mode="json")


def test_golden_the_batch_projects_to_the_counts_the_groups_and_the_rows_oldest_first() -> (
    None
):
    projection = project(BATCH)
    assert projection["counts"] == GOLDEN_COUNTS
    assert projection["groups"] == GOLDEN_GROUPS
    assert list(projection["rows"]) == GOLDEN_ROW_ORDER
    assert projection["rows"]["omnimarket#5843"]["companions"] == ["overlays#289"]
    assert projection["as_of"] == "2026-11-12T01:25:00Z"


def test_golden_a_change_read_twice_keeps_its_newest_reading() -> None:
    newer = reading(
        repository="overlays",
        number=301,
        title="text steps ordered local first",
        merged_at="2026-11-11T21:17:00Z",
        state="running",
        gap_class="none",
        read_at="2026-11-12T02:25:00Z",
    )
    projection = project({**BATCH, "readings": [*READINGS, newer]})
    assert projection["rows"]["overlays#301"]["state"] == "running"
    assert projection["counts"]["merged"] == 5
    assert projection["counts"]["seen_running"] == 2
    assert projection["counts"]["oldest_unintegrated"]["key"] == "omnimarket#5843"
    # The named break: the older reading delivered last still loses to the newer one.
    reversed_order = project({**BATCH, "readings": [newer, *READINGS]})
    assert reversed_order["rows"]["overlays#301"]["state"] == "running"


def test_golden_an_empty_batch_projects_to_zero_with_no_oldest() -> None:
    projection = project({**BATCH, "readings": []})
    assert projection["counts"]["merged"] == 0
    assert projection["counts"]["oldest_unintegrated"] is None
    assert projection["groups"] == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"gap_class": "none"}, "names what it would take"),
        ({"gap": ""}, "names its gap and its one step"),
        ({"step": ""}, "names its gap and its one step"),
        ({"state": "running"}, "has no gap class"),
        ({"merged_at": "2026-11-11T21:17:00"}, "time zone"),
        ({"state": "deployed"}, "Input should be"),
        ({"surprise": 1}, "Extra inputs"),
    ],
    ids=[
        "unintegrated-with-no-class",
        "no-gap",
        "no-step",
        "running-with-a-class",
        "bare-time",
        "unknown-state",
        "unknown-field",
    ],
)
def test_error_chain_a_reading_that_breaks_the_rule_is_refused_typed(
    change: dict[str, Any], message: str
) -> None:
    bad = {**READINGS[0], **change}
    with pytest.raises(ValidationError, match=message):
        ModelIntegrationReading.model_validate(bad)
    with pytest.raises(ValidationError):
        ModelIntegrationReadingBatch.model_validate({**BATCH, "readings": [bad]})


def test_the_groups_follow_the_declaration_order_of_the_gap_classes() -> None:
    order = [
        c.value
        for c in EnumIntegrationGapClass
        if c is not EnumIntegrationGapClass.NONE
    ]
    assert [g["gap_class"] for g in GOLDEN_GROUPS] == [
        c for c in order if c in {g["gap_class"] for g in GOLDEN_GROUPS}
    ]
    assert list(GOLDEN_COUNTS["by_gap_class"]) == order
