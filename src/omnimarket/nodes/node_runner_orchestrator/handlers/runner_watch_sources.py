# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only effect boundaries for listener binaries, fleet snapshots and releases."""

from __future__ import annotations

import subprocess
from typing import cast
from urllib.request import Request, urlopen


def read_releases(url: str, timeout: float) -> bytes:
    """Read the deployment's actions/runner releases endpoint; never write to GitHub."""
    request = Request(url, headers={"Accept": "application/vnd.github+json"})
    with urlopen(request, timeout=timeout) as response:
        return cast(bytes, response.read())


def listener_version(command: tuple[str, ...], timeout: float) -> str:
    """Run the overlay's listener --version argv without interpreting shell text."""
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=True
    )
    return result.stdout.strip()


def version_key(version: str) -> tuple[int, int, int]:
    """Compare numeric release components, never lexicographic strings."""
    parts = version.removeprefix("v").split(".")
    if len(parts) != 3 or any(not part.isdecimal() for part in parts):
        raise ValueError("runner version must contain three numeric components")
    major, minor, patch = (int(part) for part in parts)
    return major, minor, patch


def canonical_version(version: str) -> str:
    return ".".join(str(component) for component in version_key(version))
