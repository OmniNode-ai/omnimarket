# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12717: a delegated test artifact must be importable under pytest.

Live run f03fd4f6-8ee8-4219-8918-ce12c45e5d1a (dev lane, omnimarket 87cf99a)
returned a ``test`` artifact that decorated one test ``@pytest.mark_unit``.
That raises ``AttributeError`` while pytest imports the module, so no test in it
is collected, yet the gate scored it 1.0 because the marker check only looked
for a correct ``pytest.mark.unit`` somewhere and the code compiles.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _check_uses_pytest_mark_unit,
)

pytestmark = pytest.mark.unit

_GOOD = """import pytest


@pytest.mark.unit
def test_a():
    assert True


@pytest.mark.unit
def test_b():
    with pytest.raises(ValueError):
        raise ValueError
"""

_TYPO = _GOOD.replace("@pytest.mark.unit\ndef test_b", "@pytest.mark_unit\ndef test_b")


def test_correct_module_still_passes() -> None:
    assert _check_uses_pytest_mark_unit(_GOOD) is None


def test_misspelled_pytest_marker_attribute_is_refused() -> None:
    reason = _check_uses_pytest_mark_unit(_TYPO)
    assert reason is not None
    assert "pytest.mark_unit" in reason


def test_misspelled_pytest_marker_in_fenced_block_is_refused() -> None:
    reason = _check_uses_pytest_mark_unit(f"```python\n{_TYPO}```")
    assert reason is not None
    assert "pytest.mark_unit" in reason


def test_any_pytest_mark_name_is_still_allowed() -> None:
    code = _GOOD.replace("def test_b", "@pytest.mark.parametrize('x', [1])\ndef test_b")
    assert _check_uses_pytest_mark_unit(code) is None
