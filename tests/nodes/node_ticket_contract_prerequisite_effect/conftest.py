# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hermetic git environment for the ticket-contract prerequisite controls (OMN-23283).

A hook run hands its caller's refs to every child: pre-commit exports
``PRE_COMMIT_TO_REF`` (a commit of the lane's own worktree) and the lab hook
pre-check exports it to every hook it runs. The controls here build throwaway
repositories and start the hook adapter as a subprocess, whose ``--head`` default
reads that variable, so a leaked ref made every adapter run exit 128 resolving a
commit the throwaway repository does not hold. Each control starts with none.
"""

from __future__ import annotations

import pytest

_HOOK_REF_ENV = (
    "PRE_COMMIT_FROM_REF",
    "PRE_COMMIT_TO_REF",
    "PRE_COMMIT_ORIGINAL_START_POINT",
    "PRE_COMMIT_SOURCE",
    "PRE_COMMIT_LOCAL_BRANCH",
    "PRE_COMMIT_REMOTE_BRANCH",
    "PRE_COMMIT_REMOTE_NAME",
    "PRE_COMMIT_REMOTE_URL",
    "PRE_COMMIT_CHECKOUT_TYPE",
    "PRE_COMMIT_COMMIT_MSG_SOURCE",
    "PR_BODY",
    "ONEX_UMBRELLA_TICKETS",
)


@pytest.fixture(autouse=True)
def _no_inherited_hook_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _HOOK_REF_ENV:
        monkeypatch.delenv(name, raising=False)
