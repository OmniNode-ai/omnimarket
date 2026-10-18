# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How a pinned transcript read ended (OMN-21129, plan task TC.6)."""

from __future__ import annotations

from enum import StrEnum


class EnumTranscriptReadStatus(StrEnum):
    """Only READ carries text. Every refusal is decided before any file is opened."""

    READ = "read"
    #: ONEX_CONTEXT_REPLAY_ROOT is unset or empty; it has no default.
    REFUSED_ROOT_UNSET = "refused_root_unset"
    #: The path is absolute, or resolves (through .. or a symlink) outside the root.
    REFUSED_OUTSIDE_ROOT = "refused_outside_root"
    #: The resolved path is not a regular file.
    NOT_FOUND = "not_found"
    #: The file is not valid UTF-8.
    DECODE_FAILED = "decode_failed"
