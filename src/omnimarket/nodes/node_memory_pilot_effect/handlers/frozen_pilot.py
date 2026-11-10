# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pilot manifest check: a pilot runs only on a frozen bar whose files still hash to it."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


class PilotNotFrozenError(Exception):
    """The frozen bar is absent, altered, or not the one the overlay names."""


def verify_frozen(pilot_dir: Path, expected_hash: str) -> str:
    """Return the frozen hash after checking every listed file; raise PilotNotFrozenError otherwise."""
    listing = pilot_dir / "FROZEN.sha256"
    if not listing.is_file():
        raise PilotNotFrozenError("FROZEN.sha256 is absent")
    raw = listing.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_hash:
        raise PilotNotFrozenError("the frozen hash does not match the overlay")
    for line in raw.decode().splitlines():
        digest, _, name = line.partition("  ")
        target = pilot_dir / name
        if (
            not target.is_file()
            or hashlib.sha256(target.read_bytes()).hexdigest() != digest
        ):
            raise PilotNotFrozenError(f"{name} changed after the freeze")
    return actual


def frozen_system_prompt(pilot_dir: Path) -> str:
    """The answering prompt the bar froze, for every system alike."""
    bar = json.loads((pilot_dir / "bar.json").read_text(encoding="utf-8"))
    prompt = bar["answering"]["system_prompt"]
    if not isinstance(prompt, str) or not prompt:
        raise PilotNotFrozenError("bar.json carries no answering prompt")
    return prompt
