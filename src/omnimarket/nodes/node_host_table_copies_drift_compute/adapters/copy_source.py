# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared copies, in memory and on disk."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from omnimarket.nodes.node_host_table_copies_drift_compute.overlay import (
    ModelCopySettings,
)
from omnimarket.nodes.node_host_table_copies_drift_compute.protocols import (
    CopyReadError,
)

_VARIABLE = re.compile(r"\$(?:\{(?P<braced>\w+)\}|(?P<bare>\w+))")


class InMemoryCopySource:
    """Copies held in memory. ``read_errors`` injects an unreadable copy by name."""

    def __init__(self, texts: Mapping[str, str]) -> None:
        self.texts = dict(texts)
        self.read_errors: dict[str, str] = {}

    def text(self, name: str) -> str:
        if name in self.read_errors:
            raise CopyReadError(f"{name}: {self.read_errors[name]}")
        if name not in self.texts:
            raise CopyReadError(f"{name}: no such copy")
        return self.texts[name]


def expand_path(path: str, environ: Mapping[str, str]) -> Path:
    """The overlay path with each ``$NAME`` or ``${NAME}`` read from the environment; an unset one refuses."""

    def replace(match: re.Match[str]) -> str:
        variable = match.group("braced") or match.group("bare")
        value = environ.get(variable, "").strip()
        if not value:
            raise CopyReadError(f"{variable} is not set")
        return value

    return Path(_VARIABLE.sub(replace, path))


class LocalCopySource:
    """The copies as files on this host, at the paths the overlay declares."""

    def __init__(
        self,
        copies: tuple[ModelCopySettings, ...],
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._copies = {copy.name: copy.path for copy in copies}
        self._environ = os.environ if environ is None else environ

    def text(self, name: str) -> str:
        if name not in self._copies:
            raise CopyReadError(f"{name}: no such copy")
        try:
            where = expand_path(self._copies[name], self._environ)
        except CopyReadError as exc:
            raise CopyReadError(f"{name}: {exc}") from None
        try:
            return where.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise CopyReadError(f"{name}: file-missing") from None
        except (OSError, UnicodeDecodeError) as exc:
            raise CopyReadError(f"{name}: {type(exc).__name__}") from None
