# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Resolve a governing product repository to its mounted checkout."""

from __future__ import annotations

import re
from pathlib import Path


def product_clone_dir(registry_root: str, repository: str) -> Path | None:
    """Return the named directory under the registry root, if present."""
    name = repository.rsplit("/", 1)[-1]
    if not registry_root or not re.fullmatch(r"[\w][\w.-]*", name):
        return None
    clone = Path(registry_root).resolve() / name
    if clone.is_symlink() or not clone.is_dir():
        return None
    return clone
