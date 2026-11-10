# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_validator_estate_reading_effect: where the repositories are read from."""

from __future__ import annotations

from typing import Protocol


class EstateSourceError(RuntimeError):
    """A repository could not be read; the row says so and never counts it as empty."""


class ProtocolEstateSource(Protocol):
    """Read a repository at a ref. Every method raises EstateSourceError when it cannot read."""

    def head(self, repo: str, ref: str) -> str: ...

    def files_containing(
        self, repo: str, ref: str, pattern: str, roots: tuple[str, ...]
    ) -> tuple[str, ...]: ...

    def read(self, repo: str, ref: str, path: str) -> str | None: ...

    def commits_citing(self, repo: str, ref: str, ticket: str) -> int: ...


__all__ = ["EstateSourceError", "ProtocolEstateSource"]
