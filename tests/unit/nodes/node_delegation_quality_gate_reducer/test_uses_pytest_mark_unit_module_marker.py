# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19524: the test-class floor accepts a module-level ``pytestmark``.

``uses_pytest_mark_unit`` was a substring test for the decorator spelling. A
module that marks every test with ``pytestmark = pytest.mark.unit``, the form
several repos use, was refused with ``TASK_MISMATCH: missing @pytest.mark.unit``
on every local rung, and the ladder climbed to a cloud rung that answered HTTP
429 (capability matrix 2026-09-25, trial t6, run 1d19a5aa).

The same substring test also accepted the marker spelled inside a comment or a
string, which marks nothing. Both halves are pinned here.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _check_uses_pytest_mark_unit,
    _evaluate_deterministic_checks,
)

pytestmark = pytest.mark.unit

_MISSING = "TASK_MISMATCH: missing @pytest.mark.unit"

_TEST_BODY = """
def test_adds() -> None:
    assert 1 + 1 == 2
"""


def _module(header: str) -> str:
    return f"import pytest\n\n{header}\n{_TEST_BODY}"


# ---------------------------------------------------------------------------
# AC1: the module marker satisfies the floor, alone or in a list.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        "pytestmark = pytest.mark.unit",
        "pytestmark = [pytest.mark.unit]",
        "pytestmark = [pytest.mark.slow, pytest.mark.unit]",
        "pytestmark = (pytest.mark.unit, pytest.mark.slow)",
        "pytestmark: list[pytest.MarkDecorator] = [pytest.mark.unit]",
        "pytestmark = pytest.mark.unit()",
    ],
)
def test_a_module_marker_satisfies_the_floor(header: str) -> None:
    assert _check_uses_pytest_mark_unit(_module(header)) is None


def test_the_module_marker_inside_a_fenced_reply_satisfies_the_floor() -> None:
    reply = f"```python\n{_module('pytestmark = pytest.mark.unit')}```\n"
    assert _check_uses_pytest_mark_unit(reply) is None


def test_a_class_level_marker_satisfies_the_floor() -> None:
    module = (
        "import pytest\n\n\nclass TestAdds:\n"
        "    pytestmark = pytest.mark.unit\n\n"
        "    def test_adds(self) -> None:\n        assert 1 + 1 == 2\n"
    )
    assert _check_uses_pytest_mark_unit(module) is None


def test_the_recorded_t6_shape_passes_the_gate_evaluator() -> None:
    """The deterministic evaluator the ladder reads, not only the helper."""
    failures, skipped, _ = _evaluate_deterministic_checks(
        _module("pytestmark = pytest.mark.unit"), ("uses_pytest_mark_unit",)
    )
    assert failures == []
    assert skipped == []


# ---------------------------------------------------------------------------
# The decorator form still passes (AC4 guards the rest of the gate).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decorator", ["@pytest.mark.unit", "@pytest.mark.unit()"])
def test_the_decorator_still_satisfies_the_floor(decorator: str) -> None:
    module = (
        f"import pytest\n\n\n{decorator}\ndef test_adds() -> None:\n    assert True\n"
    )
    assert _check_uses_pytest_mark_unit(module) is None


def test_a_decorated_test_class_satisfies_the_floor() -> None:
    module = (
        "import pytest\n\n\n@pytest.mark.unit\nclass TestAdds:\n"
        "    def test_adds(self) -> None:\n        assert True\n"
    )
    assert _check_uses_pytest_mark_unit(module) is None


# ---------------------------------------------------------------------------
# AC2: neither form present still fails.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [
        _module(""),
        _module("pytestmark = pytest.mark.slow"),
        _module("pytestmark = [pytest.mark.integration]"),
        _module("marks = pytest.mark.unit"),
        "import pytest\n\n\n@pytest.mark.slow\ndef test_adds() -> None:\n    assert True\n",
    ],
)
def test_no_unit_marker_fails(module: str) -> None:
    assert _check_uses_pytest_mark_unit(module) == _MISSING


def test_the_evaluator_still_records_the_failure() -> None:
    failures, _, _ = _evaluate_deterministic_checks(
        _module(""), ("uses_pytest_mark_unit",)
    )
    assert failures == [_MISSING]


# ---------------------------------------------------------------------------
# AC3: marker text in a comment or a string literal marks nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        "# pytestmark = pytest.mark.unit",
        "# @pytest.mark.unit",
        'NOTE = "pytestmark = pytest.mark.unit"',
        'NOTE = "@pytest.mark.unit"',
        '"""Every test here should carry @pytest.mark.unit."""',
    ],
)
def test_marker_text_in_a_comment_or_string_does_not_count(header: str) -> None:
    assert _check_uses_pytest_mark_unit(_module(header)) == _MISSING


def test_an_unparseable_module_still_ignores_comment_text() -> None:
    """The tokenize fallback also drops comments and strings."""
    broken = "import pytest\n# @pytest.mark.unit\ndef test_adds(:\n    pass\n"
    assert _check_uses_pytest_mark_unit(broken) == _MISSING


def test_an_unparseable_module_with_a_real_decorator_passes() -> None:
    broken = "import pytest\n\n@pytest.mark.unit\ndef test_adds(:\n    pass\n"
    assert _check_uses_pytest_mark_unit(broken) is None
