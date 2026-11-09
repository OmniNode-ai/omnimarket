# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A control starts without the hook-run refs of the lane that runs it (OMN-23283)."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

_LEAKED = (
    "PRE_COMMIT_FROM_REF",
    "PRE_COMMIT_TO_REF",
    "PR_BODY",
    "ONEX_UMBRELLA_TICKETS",
)


@pytest.fixture
def leaked_before_conftest(monkeypatch: pytest.MonkeyPatch) -> None:
    # Runs after the autouse conftest fixture only when requested late; the
    # real leak is the shell environment, so assert on what the control sees.
    for name in _LEAKED:
        assert name not in os.environ


def test_hook_ref_variables_are_absent_in_every_control(
    leaked_before_conftest: None,
) -> None:
    assert not [name for name in _LEAKED if name in os.environ]
