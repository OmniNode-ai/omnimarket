# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19432: the extraction marker is found in the forms a model actually writes it.

The extractor located the marker by an exact line match, so ``### Answer``,
``## ANSWER``, ``**### ANSWER**`` and ``### ANSWER:`` were all read as "no marker": the
answer was blanked as ``ambiguous_unmarked_deliverable`` and the ladder paid for another
rung to re-ask an answered question. A marker written in another case, heading level,
emphasis or with a trailing colon IS the marker the model was asked for; it is the same
declared boundary, not a guess from prose. What stays refused is a line that merely
contains the marker text among other words, and a response with no marker line at all,
which the OMN-18278 and OMN-19525 tests keep pinning.
"""

from __future__ import annotations

import pytest
from omnibase_core.models.delegation.wire import EnumDelegationOutputShape

from omnimarket.delegation.deliverable_extraction import (
    EnumDeliverableExtractionRefusal,
    ModelDeliverableContract,
    extract_deliverable,
    resolve_task_class_deliverable_contract,
)

pytestmark = pytest.mark.unit

_BODY = "- line 455: renders a missing id as None.\n- line 503: repeats it."


@pytest.fixture(name="contract")
def _contract() -> ModelDeliverableContract:
    return resolve_task_class_deliverable_contract("review", None)


@pytest.mark.parametrize(
    "marker_line",
    [
        "### ANSWER",
        "### Answer",
        "### answer",
        "## ANSWER",
        "#### ANSWER",
        "**### ANSWER**",
        "### ANSWER:",
        "  ### ANSWER  ",
        "`### ANSWER`",
    ],
)
def test_a_marker_written_in_another_form_still_locates_the_deliverable(
    contract: ModelDeliverableContract, marker_line: str
) -> None:
    extracted = extract_deliverable(f"{marker_line}\n{_BODY}", contract)

    assert extracted.refusal is None, marker_line
    assert extracted.deliverable == _BODY
    assert "ANSWER" not in extracted.deliverable.upper()


def test_the_last_marker_wins_whatever_form_each_one_takes(
    contract: ModelDeliverableContract,
) -> None:
    raw = f"### ANSWER\nfirst draft\n\n## Answer:\n{_BODY}"

    extracted = extract_deliverable(raw, contract)

    assert extracted.deliverable == _BODY


@pytest.mark.parametrize(
    "line",
    [
        "### ANSWERS",
        "### ANSWER TO THE QUESTION",
        "The ANSWER is below",
        "### An answer",
        "ANSWER",
    ],
)
def test_a_line_that_only_contains_the_marker_text_is_not_a_marker(
    contract: ModelDeliverableContract, line: str
) -> None:
    extracted = extract_deliverable(f"{line}\n{_BODY}", contract)

    assert extracted.refusal is EnumDeliverableExtractionRefusal.AMBIGUOUS_UNMARKED


def test_a_custom_declared_marker_is_still_matched_verbatim() -> None:
    contract = ModelDeliverableContract(
        output_shape=EnumDelegationOutputShape.MARKDOWN,
        min_deliverable_share=0.1,
        markers=("## Checkpoint 2026-09-19T12:40Z",),
    )
    raw = "scratch\n## Checkpoint 2026-09-19T12:40Z\n\n# Delivered\n"

    extracted = extract_deliverable(raw, contract)

    assert extracted.deliverable == "## Checkpoint 2026-09-19T12:40Z\n\n# Delivered\n"
    assert extracted.preamble_chars == len("scratch\n")
