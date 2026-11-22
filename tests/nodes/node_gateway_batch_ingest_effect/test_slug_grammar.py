# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC5: the tenant slug grammar is a full match with no folding."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_gateway_batch_ingest_effect.topic_rules import slug_is_valid


@pytest.mark.parametrize("slug", ["a", "cli-run-h051", "a" * 32, "0", "a-b", "9z"])
def test_slug_grammar_accepts(slug: str) -> None:
    assert slug_is_valid(slug)


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "a" * 33,
        "-a",
        "a-",
        "a\n",
        "a\x00",
        "A",
        "Ab",
        chr(0x212A),  # Kelvin sign, folds to k
        "\uff41",  # fullwidth a
        "\u0430",  # Cyrillic a
        "a b",
        "a_b",
        "a.b",
        None,
        7,
    ],
)
def test_slug_grammar_refuses(slug: object) -> None:
    assert not slug_is_valid(slug)
