# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The facts-first prompt shape, and the contract that says who sends it (OMN-19432).

The 60 first-run false passes of the delegation gate recovered 16 on a plain
re-ask and 33 when the prompt stated its computed facts first (knowledge-base
reports/delegation-evals/2026-09-30-wrong-answer-why). These tests pin that the
composer states only what a script can compute from the prompt, leaves the
prompt itself unchanged after the facts, and takes its class list from the
contract and from nothing in code.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.delegation.acceptance_directives import state_prompt_for_task_class
from omnimarket.inference import task_class_authority as authority
from omnimarket.inference.task_class_authority import (
    _DEFAULT_AUTHORITY_PATH,
    EnumPromptShape,
    ModelFactsFirstPromptPolicy,
    ModelTaskClassAuthority,
    resolve_facts_first_prompt_policy,
    resolve_task_class_prompt_shape,
)
from omnimarket.nodes.node_facts_first_prompt_compute.handlers import (
    handler_facts_first_prompt as module,
)
from omnimarket.nodes.node_facts_first_prompt_compute.handlers.handler_facts_first_prompt import (
    FACTS_FIRST_HEADER,
    TASK_HEADER,
    HandlerFactsFirstPrompt,
)
from omnimarket.nodes.node_facts_first_prompt_compute.models.model_facts_first_prompt_request import (
    ModelFactsFirstPromptRequest,
)

_MEASURED_FACTS_FIRST = frozenset(
    {"code_generation", "document", "research", "reasoning", "review"}
)


def render_facts_first_prompt(prompt: str, policy: ModelFactsFirstPromptPolicy) -> str:
    return (
        HandlerFactsFirstPrompt()
        .handle(ModelFactsFirstPromptRequest(prompt=prompt, policy=policy))
        .prompt
    )


def _policy():
    policy = resolve_facts_first_prompt_policy()
    assert policy is not None
    return policy


_REVIEW_PROMPT = """Review this change to `retry_delay` in src/pkg/retry.py. You must cite the line number of each defect. Answer in at most 100 words. Do not invent defects.

```diff
@@ -10,2 +10,3 @@ def retry_delay(attempt):
     base = 2
-    return base ** attempt
+    cap = 30
+    return min(base ** attempt, cap)
```"""


# ---------------------------------------------------------------------------
# The contract names the classes; code names none
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_shipped_contract_declares_the_measured_classes_facts_first() -> None:
    shipped = ModelTaskClassAuthority.model_validate(
        yaml.safe_load(_DEFAULT_AUTHORITY_PATH.read_text(encoding="utf-8"))
    )

    declared = {
        name
        for name, entry in shipped.task_classes.items()
        if entry.prompt_shape is EnumPromptShape.FACTS_FIRST
    }

    assert declared == _MEASURED_FACTS_FIRST


@pytest.mark.unit
@pytest.mark.parametrize(
    "task_class",
    ["code_review", "planning", "summarization", "test", "no_such_class"],
)
def test_a_class_the_contract_does_not_declare_gets_the_prompt_unchanged(
    task_class: str,
) -> None:
    """code_review is held back until re-measured; the others did not beat the control."""
    assert resolve_task_class_prompt_shape(task_class) is EnumPromptShape.PLAIN
    assert (
        state_prompt_for_task_class(prompt=_REVIEW_PROMPT, task_class=task_class)
        == _REVIEW_PROMPT
    )


@pytest.mark.unit
def test_code_review_declares_plain_explicitly() -> None:
    raw = yaml.safe_load(_DEFAULT_AUTHORITY_PATH.read_text(encoding="utf-8"))

    assert raw["task_classes"]["code_review"]["prompt_shape"] == "plain"


@pytest.mark.unit
def test_the_composer_follows_the_contract_not_a_class_list_in_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flip two classes in the contract and the composer flips with them."""
    raw = yaml.safe_load(_DEFAULT_AUTHORITY_PATH.read_text(encoding="utf-8"))
    raw["task_classes"]["document"]["prompt_shape"] = "plain"
    raw["task_classes"]["test"]["prompt_shape"] = "facts_first"
    flipped = ModelTaskClassAuthority.model_validate(raw)
    monkeypatch.setattr(authority, "_delegation_task_class_authority", lambda: flipped)

    assert (
        state_prompt_for_task_class(prompt=_REVIEW_PROMPT, task_class="document")
        == _REVIEW_PROMPT
    )
    assert state_prompt_for_task_class(
        prompt=_REVIEW_PROMPT, task_class="test"
    ).startswith(FACTS_FIRST_HEADER)


@pytest.mark.unit
def test_a_class_that_declares_facts_first_without_a_policy_refuses_to_load() -> None:
    raw = yaml.safe_load(_DEFAULT_AUTHORITY_PATH.read_text(encoding="utf-8"))
    del raw["facts_first_prompt"]

    with pytest.raises(ValueError, match="facts_first_prompt policy"):
        ModelTaskClassAuthority.model_validate(raw)


@pytest.mark.unit
def test_the_module_names_no_task_class() -> None:
    source = Path(module.__file__).read_text(encoding="utf-8")

    for name in (*_MEASURED_FACTS_FIRST, "code_review", "planning", "summarization"):
        assert f'"{name}"' not in source, name


# ---------------------------------------------------------------------------
# What the facts are
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_task_follows_the_facts_unchanged() -> None:
    stated = render_facts_first_prompt(_REVIEW_PROMPT, _policy())

    assert stated.startswith(FACTS_FIRST_HEADER)
    assert stated.endswith(f"{TASK_HEADER}\n{_REVIEW_PROMPT}")


@pytest.mark.unit
def test_constraints_are_the_tasks_own_sentences_in_order() -> None:
    stated = render_facts_first_prompt(_REVIEW_PROMPT, _policy())

    assert (
        "1. You must cite the line number of each defect.\n"
        "2. Answer in at most 100 words.\n"
        "3. Do not invent defects."
    ) in stated
    # Nothing is paraphrased: every numbered line is a substring of the task.
    for line in stated.split("\n"):
        if line[:3] in {"1. ", "2. ", "3. "}:
            assert line[3:] in _REVIEW_PROMPT


@pytest.mark.unit
def test_a_numeric_limit_is_stated_as_a_limit() -> None:
    stated = render_facts_first_prompt(
        "Describe the cache. Use no more than three sentences and at most 80 words.",
        _policy(),
    )

    assert "Limits the task states: no more than 3 sentences; at most 80 words." in (
        stated
    )


@pytest.mark.unit
def test_counts_are_exact() -> None:
    prompt = "You must answer.\nOne two three four five."
    stated = render_facts_first_prompt(prompt, _policy())

    assert "Task text: 2 lines, 8 words; its prose holds 2 sentences." in stated


@pytest.mark.unit
def test_identifiers_are_listed_once_in_order_and_the_rest_are_declared_absent() -> (
    None
):
    prompt = (
        "Explain `retry_delay` and HandlerRetry.run in src/pkg/retry.py, "
        "ticket OMN-1234, then retry_delay again. You must call compute(x)."
    )
    stated = render_facts_first_prompt(prompt, _policy())

    line = next(
        ln for ln in stated.split("\n") if ln.startswith("Identifiers that appear")
    )
    listed = line.split("): ", 1)[1].split(". An identifier")[0].split(", ")
    assert listed == [
        "retry_delay",
        "HandlerRetry.run",
        "src/pkg/retry.py",
        "OMN-1234",
        "compute",
    ]
    assert "(5, in order of first appearance)" in line
    assert "does not appear in the input: do not cite or invent one." in line


@pytest.mark.unit
def test_a_capped_identifier_list_does_not_claim_to_be_complete() -> None:
    policy = _policy().model_copy(update={"max_identifiers": 2})
    stated = render_facts_first_prompt(
        "You must use alpha_one, beta_two and gamma_three.", policy
    )

    assert "the first 2 of 3" in stated
    assert "does not appear in the input" not in stated


@pytest.mark.unit
def test_diff_lines_are_numbered_from_the_hunk_header() -> None:
    stated = render_facts_first_prompt(_REVIEW_PROMPT, _policy())

    assert "Diff hunk 1, @@ -10,2 +10,3 @@" in stated
    assert "new 10:      base = 2" in stated
    assert "old 11: -    return base ** attempt" in stated
    assert "new 11: +    cap = 30" in stated
    assert "new 12: +    return min(base ** attempt, cap)" in stated
    assert "a number not listed here is not a line of the input" in stated


@pytest.mark.unit
def test_code_lines_are_numbered_from_one() -> None:
    stated = render_facts_first_prompt(
        "Document this function.\n\n```python\ndef f(x):\n    return x + 1\n```",
        _policy(),
    )

    assert (
        "Code block 1 (python), 2 lines:\n1: def f(x):\n2:     return x + 1" in stated
    )


@pytest.mark.unit
def test_a_hunk_shorter_than_its_header_marks_the_input_cut() -> None:
    cut = "Review this diff.\n@@ -1,5 +1,5 @@\n a\n-b\n+c"  # header declares 5, holds 2
    stated = render_facts_first_prompt(cut, _policy())

    assert "The input is cut:" in stated
    assert "declares 5 old and 5 new lines but the input holds 2 and 2" in stated
    assert "make no claim about it" in stated


@pytest.mark.unit
def test_an_unclosed_fence_and_a_literal_marker_mark_the_input_cut() -> None:
    stated = render_facts_first_prompt(
        "Review this.\n```python\ndef f():\n    pass\n[truncated]", _policy()
    )

    assert "the code fence opened at task line 2 is never closed" in stated
    assert "the input carries the cut marker '[truncated]'" in stated


@pytest.mark.unit
def test_a_complete_hunk_is_not_reported_cut() -> None:
    assert "The input is cut" not in render_facts_first_prompt(
        _REVIEW_PROMPT, _policy()
    )


@pytest.mark.unit
def test_code_past_the_numbering_limit_is_not_numbered_and_says_so() -> None:
    policy = _policy().model_copy(update={"max_numbered_lines": 3})
    body = "\n".join(f"x{i} = {i}" for i in range(10))
    stated = render_facts_first_prompt(
        f"You must review.\n```python\n{body}\n```", policy
    )

    assert "Line numbers are not stated: the input holds 10 code lines" in stated
    assert "Numbered code lines" not in stated


@pytest.mark.unit
@pytest.mark.parametrize("prompt", ["Say hi.", "What is the capital of France?", ""])
def test_a_prompt_with_nothing_to_state_is_byte_identical(prompt: str) -> None:
    assert render_facts_first_prompt(prompt, _policy()) == prompt


@pytest.mark.unit
def test_applying_it_twice_is_applying_it_once() -> None:
    once = render_facts_first_prompt(_REVIEW_PROMPT, _policy())

    assert render_facts_first_prompt(once, _policy()) == once


@pytest.mark.unit
def test_the_numbers_it_states_are_derivable_from_the_prompt() -> None:
    """Facts are computed, never recalled: the same prompt gives the same bytes."""
    first = render_facts_first_prompt(_REVIEW_PROMPT, _policy())
    second = render_facts_first_prompt(_REVIEW_PROMPT, _policy())

    assert first == second


# ---------------------------------------------------------------------------
# The node's contract and its result
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_result_says_whether_facts_were_stated() -> None:
    handler = HandlerFactsFirstPrompt()

    stated = handler.handle(
        ModelFactsFirstPromptRequest(prompt=_REVIEW_PROMPT, policy=_policy())
    )
    untouched = handler.handle(
        ModelFactsFirstPromptRequest(prompt="Say hi.", policy=_policy())
    )

    assert stated.facts_stated is True
    assert untouched.facts_stated is False
    assert untouched.prompt == "Say hi."


@pytest.mark.unit
def test_the_contract_declares_a_pure_compute_node_with_one_terminal_event() -> None:
    contract_path = Path(module.__file__).resolve().parent.parent / "contract.yaml"
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    terminal = "onex.evt.omnimarket.facts-first-prompt-completed.v1"

    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["terminal_event"] == terminal
    assert contract["event_bus"]["publish_topics"] == [terminal]
    assert contract["handler"]["class"] == HandlerFactsFirstPrompt.__name__
