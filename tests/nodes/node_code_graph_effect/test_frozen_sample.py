# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The frozen sample of piece 1: thirty labelled entities, four known-used controls, one sha256 (OMN-22901).

The deployment's sample lives in the overlay home and is hashed there. These tests spell their own
thirty-and-four sample, so they run on every runner, and pin what a frozen sample must do:

- the freeze is a full sha256 of the canonical content, and one changed label changes it;
- a label changed after the freeze refuses the load, and a sample that contradicts itself is refused;
- the reading compares the graph's verdicts with the labels and reads NOT WORKING when the agreement
  is under the frozen bar: two flipped labels fall under a bar of 29, three flips still pass a bar of
  27 and four do not, and an unreached control fails the reading with every label agreeing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Literal

import pytest
from pydantic import ValidationError

from omnimarket.models.model_code_graph import (
    ModelCodeGraphCommand,
    ModelCodeGraphResult,
)
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_code_graph_effect.adapters.repo_sources import (
    InMemoryRepoSource,
)
from omnimarket.nodes.node_code_graph_effect.handlers.handler_code_graph import (
    HandlerCodeGraph,
)
from omnimarket.nodes.node_code_graph_effect.overlay import (
    ModelCodeGraphOverlay,
    ModelGraphControl,
    ModelGraphSample,
    ModelGraphSampleEntry,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from tests.nodes.node_code_graph_effect.code_graph_support import (
    HOST,
    REF,
    RUN_AT,
    overlay,
)
from tests.nodes.node_code_graph_effect.test_code_graph import _reading

pytestmark = pytest.mark.unit

REPO = "example_core"
USED = 15
UNUSED = 15
CONTROLS = 4
#: The bar the two-flip tests are frozen under: one disagreement passes, two fail.
BAR_TWO_FLIPS = 29
#: The bar of the deployment's sample (27 of 30): three disagreements pass, four fail.
BAR_DEPLOYED = 27
#: The controls are m00 to m03, so a flip starts after them (a control may not be labelled unused).
FIRST_FLIPPABLE = CONTROLS

Label = Literal["used", "unused"]


def _module(number: int) -> str:
    return f"src/{REPO}/m{number:02d}.py"


def _world() -> dict[str, str]:
    """Thirty modules: m00 to m14 imported by a script (used), m15 to m29 imported by nothing.

    m30 is a thirty-first module no label names; a control may point at it to be unreached.
    """
    files = {f"src/{REPO}/__init__.py": ""}
    for number in range(USED + UNUSED + 1):
        files[_module(number)] = f"VALUE = {number}\n"
    files["scripts/run.py"] = "".join(
        f"import {REPO}.m{number:02d}\n" for number in range(USED)
    )
    return files


def _entries(flip: Iterable[int] = ()) -> tuple[ModelGraphSampleEntry, ...]:
    flipped = set(flip)
    entries = []
    for number in range(USED + UNUSED):
        truth: Label = "used" if number < USED else "unused"
        label: Label = (
            ("unused" if truth == "used" else "used") if number in flipped else truth
        )
        entries.append(
            ModelGraphSampleEntry(
                repo=REPO, path=_module(number), expected=label, basis="hand: test"
            )
        )
    return tuple(entries)


def _controls(unreached: bool = False) -> tuple[ModelGraphControl, ...]:
    paths = [_module(number) for number in range(CONTROLS)]
    if unreached:
        paths[-1] = _module(USED + UNUSED)
    return tuple(
        ModelGraphControl(repo=REPO, path=path, why="known used") for path in paths
    )


def _frozen(
    *, flip: Iterable[int] = (), bar: int = BAR_TWO_FLIPS, unreached: bool = False
) -> ModelGraphSample:
    return ModelGraphSample.freeze(
        entries=_entries(flip), min_agree=bar, controls=_controls(unreached)
    )


def _overlay(sample: ModelGraphSample) -> ModelCodeGraphOverlay:
    return overlay(
        repos=[{"name": REPO, "ref": REF}],
        allowlist=[],
        shadow=None,
        sample=sample.model_dump(mode="json"),
    )


def _run(sample: ModelGraphSample) -> tuple[ModelCodeGraphResult, InMemoryLedger]:
    ledger = InMemoryLedger()
    handler = HandlerCodeGraph(
        overlay=_overlay(sample),
        host=HOST,
        source=InMemoryRepoSource({REPO: {REF: ("c" * 40, _world())}}),
        appender=ledger,
        clock=lambda: RUN_AT,
    )
    return handler.handle(ModelCodeGraphCommand(correlation_id="s")), ledger


def _canonical_sha256(sample: ModelGraphSample) -> str:
    """The hash recomputed here, from the documented canonical form, not from the model's method."""
    text = json.dumps(
        {
            "entries": [entry.model_dump(mode="json") for entry in sample.entries],
            "min_agree": sample.min_agree,
            "controls": [
                control.model_dump(mode="json") for control in sample.controls
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- AC1: thirty entities, four controls, one sha256 ---------------------------------------------


def test_the_sample_is_thirty_entities_and_four_controls_under_a_full_sha256() -> None:
    sample = _frozen()
    assert len(sample.entries) == USED + UNUSED == 30
    assert len(sample.controls) == CONTROLS == 4
    assert len(sample.frozen_hash) == 64
    assert sample.frozen_hash == _canonical_sha256(sample)
    assert sample.content_hash() == sample.frozen_hash


def test_one_changed_label_changes_the_sha256() -> None:
    assert _frozen().frozen_hash != _frozen(flip=[FIRST_FLIPPABLE]).frozen_hash


def test_one_changed_bar_changes_the_sha256() -> None:
    assert _frozen(bar=29).frozen_hash != _frozen(bar=28).frozen_hash


def test_a_label_changed_after_the_freeze_refuses_the_load() -> None:
    document = _frozen().model_dump(mode="json")
    document["entries"][FIRST_FLIPPABLE]["expected"] = "unused"
    with pytest.raises(ValidationError, match="frozen hash"):
        overlay(repos=[{"name": REPO, "ref": REF}], sample=document)


def test_a_sample_that_labels_one_module_twice_is_refused() -> None:
    entries = (*_entries(), _entries()[0].model_copy(update={"expected": "unused"}))
    with pytest.raises(ValueError, match="twice"):
        ModelGraphSample.freeze(
            entries=entries, min_agree=BAR_DEPLOYED, controls=_controls()
        )


def test_a_control_labelled_unused_is_refused() -> None:
    """A control is known used; the sample may not also call it unused."""
    controls = (
        ModelGraphControl(repo=REPO, path=_module(USED), why="known used"),
        *_controls()[1:],
    )
    with pytest.raises(ValueError, match=r"control.*unused"):
        ModelGraphSample.freeze(
            entries=_entries(), min_agree=BAR_DEPLOYED, controls=controls
        )


def test_the_nightly_row_carries_the_frozen_hash_and_the_bar() -> None:
    sample = _frozen()
    _, ledger = _run(sample)
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.cells["sample_hash"] == sample.frozen_hash[:16]
    assert row.cells["sample_bar"] == str(BAR_TWO_FLIPS)
    assert row.cells["sample"] == "30/30"
    assert row.cells["controls"] == "4/4"


# --- AC2: the reading compares the verdicts with the labels --------------------------------------


def test_the_graph_agrees_with_every_label_and_the_reading_is_working() -> None:
    result, ledger = _run(_frozen())
    assert result.verdict is EnumReadingVerdict.WORKING
    assert _reading(ledger, hours_after_run=2) is EnumReadingVerdict.WORKING


def test_one_flipped_label_stays_at_the_bar() -> None:
    """The positive control of the two-flip test: 29 of 30 meets a bar of 29."""
    result, _ = _run(_frozen(flip=[FIRST_FLIPPABLE]))
    assert result.verdict is EnumReadingVerdict.WORKING


def test_two_flipped_labels_fall_under_the_bar_and_the_reading_reads_not_working() -> (
    None
):
    """Named mutation: two labels flipped, and the sample frozen again under the same bar."""
    result, ledger = _run(_frozen(flip=[FIRST_FLIPPABLE, USED]))
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.reasons[0] == "below-bar:28-of-30-bar-29"
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.cells["verdict"] == "NOT_WORKING"
    assert row.cells["sample"] == "28/30"
    assert _reading(ledger, hours_after_run=2) is EnumReadingVerdict.NOT_WORKING


@pytest.mark.parametrize(
    ("flips", "verdict"),
    [
        (3, EnumReadingVerdict.WORKING),
        (4, EnumReadingVerdict.NOT_WORKING),
    ],
)
def test_the_deployed_bar_of_twenty_seven_tolerates_three_flips_and_not_four(
    flips: int, verdict: EnumReadingVerdict
) -> None:
    result, _ = _run(
        _frozen(flip=range(FIRST_FLIPPABLE, FIRST_FLIPPABLE + flips), bar=BAR_DEPLOYED)
    )
    assert result.verdict is verdict


def test_an_unreached_control_fails_the_reading_with_every_label_agreeing() -> None:
    result, ledger = _run(_frozen(unreached=True))
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.reasons[0] == "controls-unreached:1"
    assert _reading(ledger, hours_after_run=2) is EnumReadingVerdict.NOT_WORKING


def test_a_label_naming_a_module_the_graph_cannot_find_is_a_disagreement() -> None:
    """A renamed module may not pass as agreement: it counts against the bar."""
    sample = _frozen()
    gone = sample.entries[0].model_copy(update={"path": _module(99)})
    moved = ModelGraphSample.freeze(
        entries=(gone, *sample.entries[1:]),
        min_agree=BAR_TWO_FLIPS,
        controls=sample.controls,
    )
    result, _ = _run(moved)
    assert result.verdict is EnumReadingVerdict.WORKING
    twice = ModelGraphSample.freeze(
        entries=(
            gone,
            sample.entries[1].model_copy(update={"path": _module(98)}),
            *sample.entries[2:],
        ),
        min_agree=BAR_TWO_FLIPS,
        controls=sample.controls,
    )
    result, _ = _run(twice)
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert any("sample-missing" in reason for reason in result.reasons)
