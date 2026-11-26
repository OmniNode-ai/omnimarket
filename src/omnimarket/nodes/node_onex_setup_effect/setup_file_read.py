# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read one bounded setup file, importing nothing else (OMN-26688).

Kept apart from handler_setup_environment, whose imports bring in the
inference stack and omnibase_infra: the delegation commands need only this
read, and importing that module made `onex delegation status` take 2.4-3.1 s
(measured 2026-11-26; 0.6 s without it).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def _read(path: Path) -> str | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("Expected a bounded regular setup file") from None
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError("Expected a bounded regular setup file")
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Expected a bounded regular setup file")
    return raw.decode()
