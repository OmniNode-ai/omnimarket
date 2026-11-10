# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests of parse_ticket_id: the accepted ids, the stripped whitespace, and the refusals."""

from __future__ import annotations

import pytest

import ticket_ids


def test_valid_ids_split_into_prefix_and_number() -> None:
    assert ticket_ids.parse_ticket_id("OMN-23424") == ("OMN", 23424)
    assert ticket_ids.parse_ticket_id("AB-7") == ("AB", 7)


def test_surrounding_whitespace_is_stripped() -> None:
    assert ticket_ids.parse_ticket_id("  OMN-23424\t") == ("OMN", 23424)
    assert ticket_ids.parse_ticket_id("\nAB-9\n") == ("AB", 9)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "omn-23424",
        "OMN-",
        "OMN-23424x",
    ],
)
def test_invalid_inputs_raise_value_error(value: str) -> None:
    with pytest.raises(ValueError):
        ticket_ids.parse_ticket_id(value)
