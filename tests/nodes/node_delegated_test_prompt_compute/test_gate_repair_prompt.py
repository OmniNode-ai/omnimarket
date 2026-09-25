# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19527 — the repair prompt for a test that passed but failed the gates."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegated_test_prompt_compute.handlers.handler_delegated_test_prompt import (
    DelegatedTestPromptRefusedError,
    build_prompt_bundle,
)
from tests.nodes.node_delegated_test_prompt_compute.test_handler_delegated_test_prompt import (
    HIDDEN,
    TEST_PATH,
    _request,
)

pytestmark = pytest.mark.unit

PREVIOUS = "import os\n\ndef test_refuses() -> None:\n    assert True\n"
FINDINGS = (
    f"{TEST_PATH}:1: [ruff_check] F401 `os` imported but unused\n"
    f"{TEST_PATH}: [infra] nothing"
)


def test_the_gate_repair_prompt_carries_the_findings_verbatim_and_the_test() -> None:
    prompt = build_prompt_bundle(
        _request(mode="repair", previous_test=PREVIOUS, gate_findings=FINDINGS)
    ).prompt
    assert FINDINGS in prompt
    assert PREVIOUS.rstrip() in prompt
    assert "PASSED" in prompt
    assert "keep every assertion" in prompt
    # It is not the pytest-failure repair.
    assert "did not pass" not in prompt


def test_a_repair_needs_a_failure_or_gate_findings() -> None:
    with pytest.raises(ValueError, match="gate findings"):
        _request(mode="repair", previous_test=PREVIOUS)


def test_a_forbidden_fragment_in_the_findings_is_refused() -> None:
    with pytest.raises(DelegatedTestPromptRefusedError):
        build_prompt_bundle(
            _request(
                mode="repair", previous_test=PREVIOUS, gate_findings=f"x {HIDDEN[1]}"
            )
        )


def test_a_write_prompt_ignores_gate_findings() -> None:
    prompt = build_prompt_bundle(_request(gate_findings=FINDINGS)).prompt
    assert FINDINGS not in prompt
