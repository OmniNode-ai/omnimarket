# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22213: a labelled diff-side line citation (``Line: new 5``) is a line citation.

The ``review`` class's ``cites_specific_lines`` rule accepted ``line N``, ``lines N``,
``LN``, ``:N`` and a labelled ``Line: N`` field, but not a label whose number is
prefixed by the diff side it refers to (``Line: new 5`` / ``Line: old 4``). Runs
9ed1a43c (cloud-gemini-2-5-flash) and 5b5b01e6 (local-studio-planner, refused on
three attempts) were judge-accepted reviews of the same diff: both name the
``start = page_number * page_size`` bug with ``page_number=1`` and cite it as
``Line: new 5``, and both were refused ``TASK_MISMATCH: missing specific line
citations``.

The fixtures below are scrubbed reconstructions of those two answers: the finding,
the trigger and the citation form are the ones the recorded answers carry; the
surrounding wording is shortened. The rule keeps its veto: an answer that names no
line number is still refused.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers import (
    handler_quality_gate,
)

pytestmark = pytest.mark.unit

_REFUSAL = "TASK_MISMATCH: missing specific line citations"

# Scrubbed answer of run 9ed1a43c (review, cloud-gemini-2-5-flash).
_RECEIPT_9ED1A43C = (
    "**Bug: page 1 skips the first page of items**\n"
    "- Line: new 5\n"
    "- Trigger: `page_number=1`\n"
    "- Why: `start = page_number * page_size` drops the `- 1` of the 1-based "
    "contract, so page 1 starts at `page_size` and returns page 2's items.\n"
)

# Scrubbed answer of run 5b5b01e6 (review, local-studio-planner).
_RECEIPT_5B5B01E6 = (
    "1. Off-by-one in the start index\n"
    "   Line: new 5\n"
    "   Input: page_number=1\n"
    "   `start = page_number * page_size` should be "
    "`(page_number - 1) * page_size`; the first page is never returned.\n"
)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("Line: new 5 drops the -1", id="line_new_form-plain"),
        pytest.param("**Line:** new 5 drops the -1", id="line_new_form-bold"),
        pytest.param("Line: old 4 had the -1", id="line_new_form-old-side"),
        pytest.param("line #new 5", id="line_new_form-hash"),
    ],
)
def test_line_new_form_satisfies_the_rule(answer: str) -> None:
    assert handler_quality_gate._check_cites_specific_lines(answer) is None


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(_RECEIPT_9ED1A43C, id="receipt_9ed1a43c"),
        pytest.param(_RECEIPT_5B5B01E6, id="receipt_9ed1a43c-5b5b01e6"),
    ],
)
def test_receipt_9ed1a43c_and_5b5b01e6_pass(answer: str) -> None:
    assert handler_quality_gate._check_cites_specific_lines(answer) is None


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("the function returns a value", id="no_citation_refused-plain"),
        pytest.param("Line: new code drops the -1", id="no_citation_refused-new-word"),
        pytest.param("Line: new", id="no_citation_refused-label-only"),
        pytest.param("the new line is wrong", id="no_citation_refused-new-line"),
    ],
)
def test_no_citation_refused(answer: str) -> None:
    assert handler_quality_gate._check_cites_specific_lines(answer) == _REFUSAL
