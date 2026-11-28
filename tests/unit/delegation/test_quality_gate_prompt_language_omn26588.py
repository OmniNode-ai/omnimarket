# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26588: an unfenced or untagged answer is parsed as the language the prompt asked for.

OMN-14004 made a fenced block's own tag select its parser. An answer with no fence,
or a fence with no tag, still fell back to Python whatever the prompt asked for, so
a correct YAML or JSON answer to "write a YAML contract fragment" was refused as
``does not compile as Python``. The prompt (the gate's grounding source) now names
the language: when it names exactly one of YAML or JSON and does not name Python,
that parser reads the unfenced or untagged answer. A prompt that names Python, names
both, or names neither keeps the Python parse, byte for byte.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)

_CHECKS = ("compiles_without_errors", "code_artifact_present")

_YAML_ANSWER = (
    "handler_routing:\n"
    "  routing_strategy: operation_match\n"
    "  handlers:\n"
    "    - operation: run_export\n"
)
_JSON_ANSWER = '{"routing_strategy": "operation_match", "handlers": ["run_export"]}'
_PYTHON_ANSWER = "def add(a: int, b: int) -> int:\n    return a + b\n"

_YAML_PROMPT = "Write a YAML contract fragment that routes the run_export operation."
_JSON_PROMPT = "Return the routing config as JSON with a handlers list."


def _failures(content: str, prompt: str | None) -> tuple[str, ...]:
    return quality_gate_delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="code_generation",
            llm_response_content=content,
            dod_deterministic=_CHECKS,
        ),
        grounding_source=prompt,
    ).failure_reasons


@pytest.mark.unit
def test_unfenced_yaml_answer_to_a_yaml_prompt_passes() -> None:
    assert _failures(_YAML_ANSWER, _YAML_PROMPT) == ()


@pytest.mark.unit
def test_untagged_fence_yaml_answer_to_a_yaml_prompt_passes() -> None:
    assert _failures(f"```\n{_YAML_ANSWER}```\n", _YAML_PROMPT) == ()


@pytest.mark.unit
def test_unfenced_json_answer_to_a_json_prompt_passes() -> None:
    assert _failures(_JSON_ANSWER, _JSON_PROMPT) == ()


@pytest.mark.unit
def test_invalid_yaml_to_a_yaml_prompt_still_fails_as_yaml() -> None:
    reasons = _failures("handlers: [unclosed\n  key: : value\n", _YAML_PROMPT)
    assert any("does not compile as YAML" in r for r in reasons), reasons


@pytest.mark.unit
def test_invalid_json_to_a_json_prompt_still_fails_as_json() -> None:
    reasons = _failures('{"handlers": [', _JSON_PROMPT)
    assert any("does not compile as JSON" in r for r in reasons), reasons


@pytest.mark.unit
def test_prose_to_a_yaml_prompt_is_still_no_artifact() -> None:
    reasons = _failures("Sure, here is the contract you asked for.", _YAML_PROMPT)
    assert any("no code artifact" in r for r in reasons), reasons


@pytest.mark.unit
def test_a_python_prompt_keeps_the_python_parse() -> None:
    prompt = "Write a Python function add(a, b) and update contract.yaml."
    assert _failures(_PYTHON_ANSWER, prompt) == ()
    reasons = _failures(_YAML_ANSWER, prompt)
    assert any("does not compile as Python" in r for r in reasons), reasons


@pytest.mark.unit
def test_a_prompt_naming_both_languages_keeps_the_python_parse() -> None:
    reasons = _failures(_YAML_ANSWER, "Convert the YAML to JSON.")
    assert any("does not compile as Python" in r for r in reasons), reasons


@pytest.mark.unit
def test_no_prompt_keeps_the_python_parse() -> None:
    assert _failures(_PYTHON_ANSWER, None) == ()
    reasons = _failures(_YAML_ANSWER, None)
    assert any("does not compile as Python" in r for r in reasons), reasons
