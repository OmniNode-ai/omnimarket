# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where a session must start for the SessionStart row to act (OMN-22016, TC.11, decision D7).

The row acts only when the session's starting directory is inside the workspace registry or an
internal-library clone. The registry root is ``OMNI_HOME`` and each internal-library clone is the
absolute path in a ``*_INTERNAL_HOME`` variable, the same roots the canonical clone sync reads.
With no root known, or no directory in the hook input, nothing is inside: the row stays silent.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

REGISTRY_HOME_ENV = "OMNI_HOME"
CLONE_HOME_SUFFIX = "_INTERNAL_HOME"


class AdditiveStartScope:
    """contains(cwd) -> whether the directory is inside the registry or an internal-library clone."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def roots(self) -> tuple[Path, ...]:
        homes = [self._environ.get(REGISTRY_HOME_ENV, "")]
        homes += [
            value
            for name, value in sorted(self._environ.items())
            if name.endswith(CLONE_HOME_SUFFIX)
        ]
        return tuple(Path(h).resolve() for h in homes if h and Path(h).is_absolute())

    def contains(self, cwd: object) -> bool:
        if not isinstance(cwd, str) or not cwd:
            return False
        directory = Path(cwd).resolve()
        return any(directory.is_relative_to(root) for root in self.roots())
