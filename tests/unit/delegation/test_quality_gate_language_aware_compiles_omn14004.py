# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14004: ``compiles_without_errors`` must not assume Python for every
``code_generation`` ask.

Found by the ratchet-canary (OMN-14000): a `code_generation` request asking for
a YAML contract fragment was false-rejected because `_check_compiles_without_errors`
unconditionally ran `ast.parse` on the candidate, regardless of what artifact
language was actually requested. This suite locks in the fix: a fenced block's
own language tag (```yaml``/```json``) selects the parser for THAT block; an
untagged fence or raw (non-fenced) content keeps the original Python-only
behavior unchanged.
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


def _gate_input(
    *, content: str, deterministic: tuple[str, ...]
) -> ModelQualityGateInput:
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="code_generation",
        llm_response_content=content,
        dod_deterministic=deterministic,
    )


@pytest.mark.unit
def test_yaml_fenced_code_generation_answer_passes_compiles_without_errors() -> None:
    """A YAML contract fragment is a valid code_generation artifact (OMN-14004)."""
    content = (
        "```yaml\n"
        "handler_routing:\n"
        "  routing_strategy: operation_match\n"
        "  handlers:\n"
        "    - operation: run_export\n"
        "      handler:\n"
        "        name: HandlerExport\n"
        "        module: omnimarket.nodes.node_export.handlers.handler_export\n"
        "```\n"
    )
    result = quality_gate_delta(
        _gate_input(content=content, deterministic=("compiles_without_errors",))
    )
    assert result.failure_reasons == ()


@pytest.mark.unit
def test_malformed_yaml_fenced_answer_still_fails_compiles_without_errors() -> None:
    content = "```yaml\nhandler_routing: [unterminated\n```\n"
    result = quality_gate_delta(
        _gate_input(content=content, deterministic=("compiles_without_errors",))
    )
    assert any(
        "does not compile as YAML" in reason for reason in result.failure_reasons
    )


@pytest.mark.unit
def test_json_fenced_code_generation_answer_passes_compiles_without_errors() -> None:
    content = '```json\n{"operation": "run_export", "handler": "HandlerExport"}\n```\n'
    result = quality_gate_delta(
        _gate_input(content=content, deterministic=("compiles_without_errors",))
    )
    assert result.failure_reasons == ()


@pytest.mark.unit
def test_malformed_json_fenced_answer_still_fails_compiles_without_errors() -> None:
    content = '```json\n{"operation": "run_export",}\n```\n'
    result = quality_gate_delta(
        _gate_input(content=content, deterministic=("compiles_without_errors",))
    )
    assert any(
        "does not compile as JSON" in reason for reason in result.failure_reasons
    )


@pytest.mark.unit
def test_python_fenced_answer_unaffected_by_language_aware_check() -> None:
    """Regression guard: a ```python`` fence keeps the original ast.parse path."""
    content = "```python\ndef normalize(value):\n    return value.strip()\n```\n"
    result = quality_gate_delta(
        _gate_input(content=content, deterministic=("compiles_without_errors",))
    )
    assert result.failure_reasons == ()


@pytest.mark.unit
def test_untagged_fenced_answer_still_falls_back_to_python_parse() -> None:
    """Regression guard: no language tag keeps the prior Python-only behavior."""
    content = "```\nhandler_routing: [unterminated\n```\n"
    result = quality_gate_delta(
        _gate_input(content=content, deterministic=("compiles_without_errors",))
    )
    assert any(
        "does not compile as Python" in reason for reason in result.failure_reasons
    )


@pytest.mark.unit
def test_raw_non_fenced_answer_still_uses_python_parse() -> None:
    """Regression guard: no fenced blocks at all keeps the prior raw-content path."""
    content = "def normalize(value):\n    return value.strip()\n"
    result = quality_gate_delta(
        _gate_input(content=content, deterministic=("compiles_without_errors",))
    )
    assert result.failure_reasons == ()
