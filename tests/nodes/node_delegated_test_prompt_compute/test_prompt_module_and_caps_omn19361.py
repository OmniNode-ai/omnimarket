# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19361 AC7 and AC8 -- the module-name and cap helpers of the prompt compute.

Written by lab delegation (onex delegate, deployed dev lane, run
42d47aea-4fea-4a87-b7d5-e76b0afb3503, Qwen3.8-27B, task class test) and judged
against the two criteria and their planted mutations; only ruff autofix and
formatting were applied afterwards.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegated_test_prompt_compute.handlers.handler_delegated_test_prompt import (
    _cap,
    _module_name,
    build_prompt_bundle,
)
from omnimarket.nodes.node_delegated_test_prompt_compute.models.model_delegated_test_prompt import (
    MAX_PREVIOUS_TEST_CHARS,
    ModelDelegatedTestPromptRequest,
    ModelFailureContext,
)


@pytest.mark.unit
def test_module_name_regular_file():
    assert _module_name("src/omnimarket/a/b.py") == "omnimarket.a.b"


@pytest.mark.unit
def test_module_name_init_file():
    assert _module_name("src/omnimarket/a/__init__.py") == "omnimarket.a"


@pytest.mark.unit
def test_cap_exact_limit_untouched():
    text = "a" * 10
    result, truncated = _cap(text, 10)
    assert result == text
    assert truncated is False


@pytest.mark.unit
def test_cap_one_over_limit_truncated():
    text = "a" * 11
    result, truncated = _cap(text, 10)
    assert result == "a" * 10 + "\n# ... truncated ...\n"
    assert truncated is True


@pytest.mark.unit
def test_cap_empty_text():
    result, truncated = _cap("", 5)
    assert result == ""
    assert truncated is False


@pytest.mark.unit
def test_cap_zero_limit_with_text():
    text = "abc"
    result, truncated = _cap(text, 0)
    assert result == "\n# ... truncated ...\n"
    assert truncated is True


@pytest.mark.unit
def test_build_prompt_bundle_write_prompt_contains_module_name():
    request = ModelDelegatedTestPromptRequest(
        mode="write",
        criterion="AC7",
        target_path="src/omnimarket/a/b.py",
        target_excerpt="def foo(): pass",
        test_path="tests/test_foo.py",
    )
    bundle = build_prompt_bundle(request)
    assert "import it as `omnimarket.a.b`" in bundle.prompt


@pytest.mark.unit
def test_build_prompt_bundle_write_prompt_init_module():
    request = ModelDelegatedTestPromptRequest(
        mode="write",
        criterion="AC7",
        target_path="src/omnimarket/a/__init__.py",
        target_excerpt="def bar(): pass",
        test_path="tests/test_bar.py",
    )
    bundle = build_prompt_bundle(request)
    assert "import it as `omnimarket.a`" in bundle.prompt


@pytest.mark.unit
def test_build_prompt_bundle_repair_prompt_caps_previous_test():
    previous_test = "x" * (MAX_PREVIOUS_TEST_CHARS + 50)
    failure = ModelFailureContext(
        outcome="failed", exception_type="AssertionError", message="assert False"
    )
    request = ModelDelegatedTestPromptRequest(
        mode="repair",
        criterion="AC8",
        target_path="src/omnimarket/a/b.py",
        target_excerpt="def foo(): pass",
        test_path="tests/test_foo.py",
        previous_test=previous_test,
        failure=failure,
    )
    bundle = build_prompt_bundle(request)
    # The prompt should contain the truncated version, not the full string
    truncated_text, _ = _cap(previous_test, MAX_PREVIOUS_TEST_CHARS)
    assert truncated_text in bundle.prompt
    # The full untruncated string should NOT be in the prompt
    assert previous_test not in bundle.prompt
    # The truncation marker should be present
    assert "# ... truncated ..." in bundle.prompt
