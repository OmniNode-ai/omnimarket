# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19362 — the WRITE/REPAIR reply parser."""

from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_delegated_test_loop_orchestrator import parse_test_reply

pytestmark = pytest.mark.unit

PATH = "tests/unit/test_dtl_generated_x.py"
SOURCE = "def test_ok():\n    assert 1 + 1 == 2\n"


def _reply(**fields: object) -> str:
    return json.dumps({"test_path": PATH, "test_source": SOURCE, **fields})


@pytest.mark.parametrize(
    "text",
    [
        _reply(),
        f"```json\n{_reply()}\n```",
        f"Here is the test:\n{_reply()}\nThat is all.",
    ],
)
def test_a_json_reply_in_any_wrapping_yields_the_source(text: str) -> None:
    assert parse_test_reply(text, PATH) == (SOURCE, "")


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("no json here", "not a JSON object"),
        (_reply(test_source=""), "no test_source"),
        (_reply(test_path="tests/other.py"), "is not"),
        (_reply(test_source="def test(:\n"), "is not valid Python"),
    ],
)
def test_an_unusable_reply_names_its_reason(text: str, reason: str) -> None:
    source, why = parse_test_reply(text, PATH)
    assert source == ""
    assert reason in why
