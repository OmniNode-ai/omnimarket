# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether the delegation hooks' switch (POST_SKILL_DELEGATION_ENFORCER) is on."""

import os
import stat
from pathlib import Path

from omnibase_core.enums.enum_hook_bit import EnumHookBit, hook_enabled

__all__ = ["delegation_switch_enabled"]


def delegation_switch_enabled() -> bool:
    """Read the effective mask the hooks read: the environment, else ~/.omnibase/.env."""
    bit = EnumHookBit.POST_SKILL_DELEGATION_ENFORCER
    if "ONEX_HOOKS_MASK" in os.environ:
        return hook_enabled(bit)
    path = Path(
        os.environ.get("OMNIBASE_ENV_FILE", str(Path.home() / ".omnibase" / ".env"))
    )
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return hook_enabled(bit)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError("Unsafe hook mask source")
        raw = source.read(1024 * 1024 + 1).decode()
    for line in raw.splitlines():
        if line.strip().startswith("ONEX_HOOKS_MASK="):
            try:
                mask = int(line.strip().split("=", 1)[1], 0)
            except ValueError:
                return hook_enabled(bit)
            return hook_enabled(bit, mask=mask)
    return hook_enabled(bit)
