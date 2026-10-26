# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20910: a labelled line citation (``Line: 5``, ``**Line:** 5``) is a line citation.

Measured 2026-10-26 on the h201 delegation projection (``delegation_events``, last
30 days): the ``review`` class's ``cites_specific_lines`` rule refused three answers
to the same diff review ("Name each correctness bug you find, the line it is on ...")
for ``TASK_MISMATCH: missing specific line citations`` although each names its line
as a labelled field: run ef0e0d15-a5d3-422a-ad12-7b54ad85c70a and run
4362a15c-5530-454f-af33-768d43af6a0d (nvidia/nemotron-3-ultra-550b-a55b:free,
``- **Line:** 5``) and run 80dcfc6f-f20e-4965-9199-8971af6eb6e4
(mistralai/mistral-nemo, ``- Line: 9 (new)``). Line 5 of the new file is the
``start = page_number * page_size`` line both Ultra answers name.

The citation regex matched ``line 5``, ``L5`` and ``:5`` but not a ``line`` label
followed by a colon or ``#`` (with optional markdown emphasis) and then the number.
The rule keeps its veto: an answer that names no line number is still refused.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers import (
    handler_quality_gate,
)

pytestmark = pytest.mark.unit

_REFUSAL = "TASK_MISMATCH: missing specific line citations"

# Verbatim response_text of run ef0e0d15 (review, byok-openrouter serving
# nvidia/nemotron-3-ultra-550b-a55b:free, 2026-10-25), refused on both attempts.
_ULTRA_EF0E0D15 = (
    "**Bug 1: Off-by-one error in start index calculation**  \n"
    "- **Line:** 5 (`start = page_number * page_size`)  \n"
    "- **Trigger:** Any call with `page_number=1` (the first 1-based page).  \n"
    "- **Why:** The docstring promises 1-based paging, so page 1 should start at "
    "index 0. The old code used `(page_number - 1) * page_size`; the new code drops "
    "the `-1`, causing page 1 to skip the first `page_size` items and return page "
    "2's data.\n\n"
    "**Bug 2: Missing validation for `page_number`**  \n"
    "- **Line:** 5 (same line; the calculation uses `page_number` unchecked)  \n"
    "- **Trigger:** `page_number <= 0` (e.g., `page_number=0` or `page_number=-1`).  \n"
    "- **Why:** With `page_number=0`, `start=0` returns the first page (violating "
    "1-based contract). With negative `page_number`, Python's negative slicing "
    "returns items from the end of the list, which is nonsensical for paging."
)

# Verbatim opening of run 80dcfc6f's response_text (mistralai/mistral-nemo, same prompt).
_NEMO_80DCFC6F = (
    "1. **Incorrect calculation of start index**\n"
    "   - Line: 9 (new)\n"
    "   - Input trigger: When `page_number` is 1\n"
)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(_ULTRA_EF0E0D15, id="ultra-ef0e0d15-bold-label"),
        pytest.param(_NEMO_80DCFC6F, id="nemo-80dcfc6f-plain-label"),
        pytest.param("Line: 12 divides by zero", id="label-colon"),
        pytest.param("**Line**: 12 divides by zero", id="label-bold-before-colon"),
        pytest.param("Lines: 3-4 drop the guard", id="plural-label-range"),
        pytest.param("line #7 reads past the end", id="label-hash"),
        pytest.param("`Line`: 7 reads past the end", id="label-code-span"),
    ],
)
def test_a_labelled_line_number_is_a_line_citation(answer: str) -> None:
    assert handler_quality_gate._check_cites_specific_lines(answer) is None


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("Line: see the loop above", id="label-without-number"),
        pytest.param("**Line:** the slicing call", id="bold-label-without-number"),
        pytest.param("the guard in line_5_handler is wrong", id="identifier"),
        pytest.param(
            "the pipeline: 5 stages all skip validation", id="word-ending-line"
        ),
        pytest.param("the function returns a value", id="no-line-at-all"),
    ],
)
def test_an_answer_naming_no_line_number_is_still_refused(answer: str) -> None:
    assert handler_quality_gate._check_cites_specific_lines(answer) == _REFUSAL


def test_existing_citation_forms_still_pass() -> None:
    for answer in ("bug on line 42", "lines 10-20", "see L7", "paginate.py:5"):
        assert handler_quality_gate._check_cites_specific_lines(answer) is None, answer
