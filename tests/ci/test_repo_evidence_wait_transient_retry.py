# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The repo-evidence wait loops retry a transient gh api failure before refusing."""

from __future__ import annotations

from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).parent.parent.parent / ".github" / "workflows"
FILES = [
    "ci.yml",
    "dep-health-gate.yml",
    "auto-merge.yml",
    "plugin-compat-gate.yml",
    "validator-runtime-profiles.yml",
]


@pytest.mark.unit
@pytest.mark.parametrize("name", FILES)
def test_transient_gh_api_failure_is_retried(name: str) -> None:
    text = (WORKFLOWS / name).read_text()
    refuse = text.index("gh api call failed (non-rate-limit)")
    retry = text.index("transient_fails=$(( ${transient_fails:-0} + 1 ))")
    assert retry < refuse, "the retry must run before the refusal"
    assert 'if [ "$transient_fails" -lt 6 ]' in text
    assert text.count("transient_fails=0") == 1, "a good read resets the counter"
