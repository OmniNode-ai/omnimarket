# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20173: Coding Plan endpoints are unavailable to direct callers."""

import logging

import pytest

from omnimarket.inference.coding_plan_endpoint import (
    addresses_coding_plan,
    glm_url_or_empty,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("url", "blocked"),
    [
        ("https://api.z.ai/api/coding/paas/v4", True),
        ("https://other.example/API/CODING/paas/v4/?key=secret", True),
        ("http://other.example/api/coding", True),
        ("https://other.example/api/coding/?q=1", True),
        ("https://other.example/prefix/api/coding?q=1", True),
        ("https://api.z.ai/api/paas/v4", False),
        ("http://localhost:8000/v1", False),
        ("https://other.example/api/codingish/v4", False),
        ("https://other.example/v1?q=/api/coding/", False),
        ("https://api.coding.example/v1", False),
        ("", False),
    ],
)
def test_glm_url_filter(
    url: str, blocked: bool, caplog: pytest.LogCaptureFixture
) -> None:
    assert addresses_coding_plan(url) is blocked
    with caplog.at_level(logging.WARNING):
        assert glm_url_or_empty(url, source="test-reader") == ("" if blocked else url)
    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == int(blocked)
    if blocked:
        assert "test-reader" in warnings[0].getMessage()
        assert "secret" not in warnings[0].getMessage()
        assert url not in warnings[0].getMessage()
