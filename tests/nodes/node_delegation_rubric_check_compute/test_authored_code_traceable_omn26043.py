# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Code the answer itself authors is a deliverable, not a claim about the run (OMN-26043).

The two answers are the shape the glm-5.3-flash and glm-5.3 harness tiers wrote for a
code_generation request; the rubric read ``seen.add`` as a file name and the test's
sample strings as quotations of a source the model never saw.
"""

import pytest

from tests.nodes.node_delegation_rubric_check_compute.test_tool_use_rubric import (
    criterion,
    transcript,
)

pytestmark = pytest.mark.unit

PROMPT = "Write untraced_facts(facts, source) and one pytest test for it."

CODE = """```python
def untraced_facts(facts: tuple[str, ...], source: str) -> tuple[str, ...]:
    seen: set[str] = set()
    untraced: list[str] = []
    for fact in facts:
        if not fact or fact in source or fact in seen:
            continue
        seen.add(fact)
        untraced.append(fact)
    return tuple(untraced)


def test_untraced_facts() -> None:
    source = "BTC rose while ETH fell"
    assert untraced_facts(("SOL surged", "BTC rose"), source) == ("SOL surged",)
```
"""


def verdict(answer: str):
    return criterion(
        "task_answer_traceable", run=transcript(), answer=answer, prompt=PROMPT
    )


def test_authored_code_block_is_not_a_claim() -> None:
    row = verdict(CODE + "\nThe test checks that a fact absent from source is kept.")
    assert (row.outcome, row.reason_code) == ("UNDETERMINED", "no_identifiers"), row


def test_prose_beside_authored_code_is_still_traced() -> None:
    row = verdict(CODE + "\nSee plugins/omni/missing_widget.py for the caller.")
    assert row.outcome == "FAIL"
    assert "plugins/omni/missing_widget.py" in row.facts


def test_ticket_id_inside_authored_code_is_still_traced() -> None:
    row = verdict("```python\n# SYN-999 widget\nx = 1\n```\n")
    assert row.outcome == "FAIL"
    assert "SYN-999" in row.facts


def test_path_inside_authored_code_is_still_traced() -> None:
    row = verdict('```python\nOPEN = "src/phantom/widget.py"\n```\n')
    assert row.outcome == "FAIL"


def test_prose_naming_a_string_of_the_authored_code_is_traced() -> None:
    row = verdict(CODE + '\nThe first assert uses `"SOL surged"` as the untraced fact.')
    assert row.outcome == "PASS", row


def test_prose_naming_a_string_the_code_never_wrote_still_fails() -> None:
    row = verdict(CODE + '\nThe first assert uses `"DOGE moons"` as the untraced fact.')
    assert row.outcome == "FAIL"
    assert '"DOGE moons"' in row.facts or "DOGE moons" in row.facts
