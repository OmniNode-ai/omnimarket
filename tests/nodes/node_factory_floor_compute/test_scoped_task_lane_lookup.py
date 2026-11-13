# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A scoped task finds the lanes named for it through the name-word index, never by testing every lane in the ledger.

Measured on the publishing host 2026-11-13: the record step's compute spent 19 s of 34 s calling ``names_task`` 4.5 million
times (about 23,000 lanes for each of 194 task reads), so the step ran past its 45 s limit as the ledger grew.
"""

from __future__ import annotations

import pytest

from omnimarket.models.model_factory_floor_input import ModelFloorManifestTask
from omnimarket.nodes.node_factory_floor_compute.handlers import (
    floor_lanes,
    handler_factory_floor,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.floor_lanes import (
    name_tokens,
    names_task,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    _index,
    _task_rows,
)
from tests.nodes.node_factory_floor_compute.support import request, row

pytestmark = pytest.mark.unit

LANES = (
    "one-rt-t10-delegation",
    "one-rt-t1-writers",
    "t1",
    "t5",
    "one-rt-t5-writers",
    "comp-cm6-r2",
    "comp-cm12",
    "cm1",
    "ta-9b-fix",
    "ta9b",
    "unattributed:omnibook",
    "",
)
TASK_IDS = ("T5", "T10", "T1", "CM.6", "CM.1", "CM.12", "TA.9b", "one-rt", "", "x.y.z")


def words_of(lanes: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    return {lane: name_tokens(lane) for lane in lanes}


def tokens_of(lanes: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    found: dict[str, list[str]] = {}
    for lane in lanes:
        for token in dict.fromkeys(name_tokens(lane)):
            found.setdefault(token, []).append(lane)
    return {token: tuple(value) for token, value in found.items()}


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_the_index_returns_exactly_the_lanes_the_full_scan_returns(
    task_id: str,
) -> None:
    from omnimarket.nodes.node_factory_floor_compute.handlers.floor_lanes import (
        lanes_naming_task,
    )

    words = words_of(LANES)
    by_scan = {lane for lane in LANES if names_task(task_id, words[lane])}
    by_index = set(lanes_naming_task(task_id, tokens_of(LANES), words))
    assert by_index == by_scan, task_id


def test_the_index_is_not_vacuous() -> None:
    # The positive control for the parity cases above: at least one task id does name lanes, and one does not.
    from omnimarket.nodes.node_factory_floor_compute.handlers.floor_lanes import (
        lanes_naming_task,
    )

    words = words_of(LANES)
    assert lanes_naming_task("T5", tokens_of(LANES), words)
    assert lanes_naming_task("CM.6", tokens_of(LANES), words) == ("comp-cm6-r2",)
    assert lanes_naming_task("T99", tokens_of(LANES), words) == ()


def test_a_scoped_task_tests_only_the_lanes_that_carry_its_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quiet_lanes = 2000
    ledger = (
        row("CLAIM", 30, "one-rt-t5-writers", tickets=("OMN-5",)),
        row("CLAIM", 29, "another-lane-t50", tickets=("OMN-5",)),
        *(
            row("CLAIM", 20, f"noise-lane-{number}", tickets=("OMN-5",))
            for number in range(quiet_lanes)
        ),
    )
    asked = request(ledger=ledger)
    index = _index(asked)
    task = ModelFloorManifestTask(id="T5", title="T5 writers", scoped=True)
    calls: list[str] = []

    def counted(task_id: str, lane_tokens: tuple[str, ...]) -> bool:
        calls.append(task_id)
        return names_task(task_id, lane_tokens)

    monkeypatch.setattr(floor_lanes, "names_task", counted)
    if hasattr(handler_factory_floor, "names_task"):
        # The scan this replaces called the rule from the handler's own namespace.
        monkeypatch.setattr(handler_factory_floor, "names_task", counted)

    found = _task_rows(task, ("OMN-5",), asked, index)

    assert [item.lane for item in found] == ["one-rt-t5-writers"]
    assert len(calls) <= 5, f"{len(calls)} lanes tested for {quiet_lanes + 2} lanes"
