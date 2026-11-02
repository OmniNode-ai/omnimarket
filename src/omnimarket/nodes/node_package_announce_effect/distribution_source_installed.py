# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Distribution source over the interpreter's installed packages (OMN-21597)."""

import sys
from importlib.metadata import Distribution, distributions
from pathlib import Path


class DistributionSourceInstalled:
    """Reads installed distributions from metadata alone.

    With no ``paths`` it reads every distribution on the interpreter's path and
    searches the same path for contract files. Given ``paths`` it reads only
    those directories, which is how a lane points the announcer at one image layer.
    """

    def __init__(self, paths: tuple[str, ...] | None = None) -> None:
        self._paths = paths

    def distributions(self) -> tuple[Distribution, ...]:
        """Return the installed distributions."""
        if self._paths is None:
            return tuple(distributions())
        return tuple(distributions(path=list(self._paths)))

    def search_roots(self) -> tuple[Path, ...]:
        """Return the directories searched for contract files."""
        source = self._paths if self._paths is not None else tuple(sys.path)
        return tuple(Path(entry) for entry in source if entry)
