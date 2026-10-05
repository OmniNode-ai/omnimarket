# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_facts_first_prompt_compute: a prompt in, the stated prompt out.

The chain is the replay proof. A captured prompt and the shipped policy must
yield the same bytes every time, so a prompt a model was shown can be re-derived
and audited long after the run. The expected text below is the golden; a change
to what the node states is a change to this file, reviewed as one.

Related:
    - OMN-19432: ship the facts-first prompt into onex delegate
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.inference.task_class_authority import resolve_facts_first_prompt_policy
from omnimarket.nodes.node_facts_first_prompt_compute.handlers.handler_facts_first_prompt import (
    HandlerFactsFirstPrompt,
)
from omnimarket.nodes.node_facts_first_prompt_compute.models.model_facts_first_prompt_request import (
    ModelFactsFirstPromptRequest,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_facts_first_prompt_compute"
    / "contract.yaml"
)

_PROMPT = (
    "Review `total` in src/cart.py. You must cite the line number of each defect. "
    "Answer in at most 50 words.\n\n"
    "```diff\n@@ -41,2 +41,3 @@\n     result = 0\n-    return result\n"
    "+    result -= discount\n+    return result\n```"
)

_GOLDEN = (
    "Facts computed from the task text by code (exact, not a reading):\n\n"
    "- Task text: 9 lines, 38 words; its prose holds 3 sentences.\n"
    "- Limits the task states: at most 50 words.\n"
    "- Code in the input: 0 code blocks, 1 diff hunks, 4 lines.\n\n"
    "Identifiers that appear in the input (2, in order of first appearance): "
    "total, src/cart.py. An identifier not in this list does not appear in the "
    "input: do not cite or invent one.\n\n"
    "Constraints the task states, copied from it in order. Satisfy every one:\n"
    "1. You must cite the line number of each defect.\n"
    "2. Answer in at most 50 words.\n\n"
    "Numbered code lines of the input (cite these numbers; a number not listed "
    "here is not a line of the input):\n"
    "Diff hunk 1, @@ -41,2 +41,3 @@ (new-side numbers for context and added lines, "
    "old-side numbers marked old for removed lines):\n"
    "new 41:      result = 0\n"
    "old 42: -    return result\n"
    "new 42: +    result -= discount\n"
    "new 43: +    return result\n\n"
    "The task, exactly as given:\n" + _PROMPT
)


def _request() -> ModelFactsFirstPromptRequest:
    policy = resolve_facts_first_prompt_policy()
    assert policy is not None
    return ModelFactsFirstPromptRequest(prompt=_PROMPT, policy=policy)


@pytest.mark.unit
def test_golden_chain_prompt_in_stated_prompt_out() -> None:
    result = HandlerFactsFirstPrompt().handle(_request())

    assert result.prompt == _GOLDEN
    assert result.facts_stated is True


@pytest.mark.unit
def test_golden_chain_replays_to_the_same_bytes() -> None:
    handler = HandlerFactsFirstPrompt()

    assert handler.handle(_request()) == handler.handle(_request())


@pytest.mark.unit
def test_golden_chain_contract_terminal_event_is_the_one_published() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))

    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert contract["terminal_event"] == (
        "onex.evt.omnimarket.facts-first-prompt-completed.v1"
    )
