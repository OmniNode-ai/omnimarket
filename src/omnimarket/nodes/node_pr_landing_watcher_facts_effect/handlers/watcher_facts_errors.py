# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The watcher facts node's source error (OMN-23507)."""

from __future__ import annotations


class WatcherFactsSourceError(Exception):
    """The watcher state could not be read or the facts could not be written; the pass ends in the typed failed event."""


__all__: list[str] = ["WatcherFactsSourceError"]
