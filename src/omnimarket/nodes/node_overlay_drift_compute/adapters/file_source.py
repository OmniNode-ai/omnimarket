# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared files, in memory and on disk."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from omnimarket.nodes.node_overlay_drift_compute.protocols import SourceReadError

_VARIABLE = re.compile(r"\$(?:\{(?P<braced>\w+)\}|(?P<bare>\w+))")


class InMemoryFileSource:
    """Files held in memory by the overlay's spelling of their path. ``read_errors`` injects an unreadable file."""

    def __init__(self, texts: Mapping[str, str]) -> None:
        self.texts = dict(texts)
        self.read_errors: dict[str, str] = {}

    def text(self, path: str) -> str:
        if path in self.read_errors:
            raise SourceReadError(self.read_errors[path])
        if path not in self.texts:
            raise SourceReadError("file-missing")
        return self.texts[path]


def expand_path(path: str, environ: Mapping[str, str]) -> Path:
    """The overlay path with each ``$NAME`` or ``${NAME}`` read from the environment; an unset one refuses."""

    def replace(match: re.Match[str]) -> str:
        variable = match.group("braced") or match.group("bare")
        value = environ.get(variable, "").strip()
        if not value:
            raise SourceReadError(f"{variable}-not-set")
        return value

    return Path(_VARIABLE.sub(replace, path))


class LocalFileSource:
    """The declared files as files on this host."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def text(self, path: str) -> str:
        where = expand_path(path, self._environ)
        try:
            return where.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise SourceReadError("file-missing") from None
        except (OSError, UnicodeDecodeError) as exc:
            raise SourceReadError(type(exc).__name__) from None
