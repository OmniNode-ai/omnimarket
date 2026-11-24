# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from enum import StrEnum


class EnumReplayRoute(StrEnum):
    CHAT = "chat"
    WORKTREE = "worktree"


class EnumReplayRefusal(StrEnum):
    REPOSITORY_MISSING = "repository_missing"
    SOURCE_SHA_UNRECOVERABLE = "source_sha_unrecoverable"
    CHECK_MISSING = "check_missing"


class EnumReplayAcceptBasis(StrEnum):
    CHAT_GATE = "chat_gate"
    CHECK_EXIT_CODE = "check_exit_code"


__all__: list[str] = [
    "EnumReplayAcceptBasis",
    "EnumReplayRefusal",
    "EnumReplayRoute",
]
