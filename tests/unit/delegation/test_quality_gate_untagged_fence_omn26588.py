# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26588: an untagged fence beside a declared code block is not the artifact.

An untagged fence declares no language, so the compile check read it as Python. A
chat answer to a code task often holds a tagged ``python`` block that compiles and an
untagged fence that holds a shell command or a console listing (``uv run pytest ...``,
``path:63``); the compile check refused the whole answer as
``does not compile as Python`` for that fence. When the answer carries at least one
block tagged with a supported language (python, yaml, json), the tagged block is the
artifact and an untagged fence is read as prose around it. An answer whose only fences
are untagged, and an answer whose tagged block fails to parse, keep their verdicts.
"""

from __future__ import annotations

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _check_compiles_without_errors,
)

_GOOD_PYTHON = "```python\ndef add(a: int, b: int) -> int:\n    return a + b\n```\n"
_SHELL_FENCE = "```\nuv run pytest tests/x.py -q\n```\n"
_CONSOLE_FENCE = "```\nsrc/pkg/mod.py:63\nsrc/pkg/other.py:24\n```\n"


def test_untagged_shell_fence_beside_a_compiling_python_block_is_not_malformed() -> (
    None
):
    answer = _GOOD_PYTHON + "Then run:\n" + _SHELL_FENCE
    assert _check_compiles_without_errors(answer) is None


def test_untagged_console_listing_beside_a_compiling_python_block_is_not_malformed() -> (
    None
):
    answer = "Expected output:\n" + _CONSOLE_FENCE + _GOOD_PYTHON
    assert _check_compiles_without_errors(answer) is None


def test_control_an_untagged_fence_alone_that_is_not_python_is_still_malformed() -> (
    None
):
    result = _check_compiles_without_errors(_SHELL_FENCE)
    assert result is not None
    assert result.startswith("MALFORMED: response does not compile as Python")


def test_control_a_tagged_python_block_that_fails_to_parse_is_still_malformed() -> None:
    broken = "```python\ndef add(a, b)\n    return a + b\n```\n"
    result = _check_compiles_without_errors(broken + _SHELL_FENCE)
    assert result is not None
    assert result.startswith("MALFORMED: response does not compile as Python")


def test_control_an_untagged_python_fence_beside_a_yaml_block_is_not_skipped_when_the_prompt_asks_yaml() -> (
    None
):
    answer = "```\nroutes:\n  - a: [\n```\n" + "```yaml\nroutes:\n  - a: b\n```\n"
    result = _check_compiles_without_errors(
        answer, "Write a YAML contract fragment for the routes."
    )
    assert result is not None
    assert "YAML" in result
