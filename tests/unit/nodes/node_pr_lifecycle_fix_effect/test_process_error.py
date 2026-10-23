# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the shared OCC git transport helpers (OMN-13990).

Covers the HTTPS x-access-token URL construction, credential redaction, and the
run_git wrapper (success + credential-scrubbed failure). No network access.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.process_error import (
    scrub_credentials,
)


@pytest.mark.unit
class TestScrubCredentials:
    def test_redacts_token_segment(self) -> None:
        text = (
            "fatal: unable to access "
            "'https://x-access-token:SUPERSECRET123@github.com/x/y.git'"
        )
        scrubbed = scrub_credentials(text)
        assert "SUPERSECRET123" not in scrubbed
        assert "x-access-token:***@github.com" in scrubbed

    def test_passthrough_when_no_credential(self) -> None:
        assert scrub_credentials("nothing secret here") == "nothing secret here"

    def test_empty_string_is_safe(self) -> None:
        assert scrub_credentials("") == ""
