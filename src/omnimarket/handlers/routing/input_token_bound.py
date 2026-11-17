# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A tokenizer-free upper bound on the input tokens an endpoint will meter.

The router's own estimate is characters over four. A served model meters its
real tokenisation, which for identifier-dense text (digits, hashes, UUIDs,
base64, generated code with numbered names) is 1.0 to 2.0 characters per
token, so a prompt inside the estimated budget can still fill the window and,
with the backend's full ``max_tokens`` reserved, be refused by the endpoint.

``input_token_upper_bound`` never undercounts the readings taken on the lab
endpoint (``tests/nodes/node_delegation_routing_reducer/fixtures/
metered_token_readings.json``): digits, floats and punctuation count one token
each, a word of 16 or more characters that holds a digit (UUIDs, hashes,
base64, long identifiers) counts one token per byte, a non-ASCII byte counts
one token, whitespace counts one token per character, and only the remaining
ASCII letters are discounted, at two per token. A tier that declares
``max_input_token_bound`` in ``routing_tiers.yaml`` is skipped for a prompt
whose bound is above it, so the window is never overrun whatever the density.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from pathlib import Path

import yaml

__all__ = [
    "TIER_INPUT_BOUND_KEY",
    "input_token_upper_bound",
    "tier_input_bound_ceiling",
]

TIER_INPUT_BOUND_KEY = "max_input_token_bound"
# A whitespace-delimited word at least this long that holds a digit is treated
# as an identifier, hash or encoded blob: one token per byte.
_DENSE_WORD_MIN_LENGTH = 16
_LETTER_TOKENS = 0.5
_WORD = re.compile(r"\S+")


def input_token_upper_bound(text: str) -> int:
    """Return a count of tokens the endpoint will not exceed for ``text``."""
    total = 0.0
    last = 0
    for match in _WORD.finditer(text):
        total += match.start() - last
        last = match.end()
        word = match.group()
        if len(word) >= _DENSE_WORD_MIN_LENGTH and any(c.isdigit() for c in word):
            total += len(word.encode())
            continue
        for char in word:
            if ord(char) > 127:
                total += len(char.encode())
            elif char.isalpha():
                total += _LETTER_TOKENS
            else:
                total += 1
    total += len(text) - last
    return math.ceil(total)


@lru_cache(maxsize=8)
def _ceilings(path: str, mtime_ns: int) -> dict[str, int]:
    del mtime_ns  # part of the cache key only: a rewritten file is re-read
    loaded = yaml.safe_load(Path(path).read_text())
    ceilings: dict[str, int] = {}
    for tier in (loaded or {}).get("tiers", []):
        value = tier.get(TIER_INPUT_BOUND_KEY)
        if value is not None:
            ceilings[str(tier["name"])] = int(value)
    return ceilings


def tier_input_bound_ceiling(config_path: Path, tier_name: str) -> int | None:
    """The input-token bound a tier declares in the ladder file, or ``None``."""
    return _ceilings(str(config_path), config_path.stat().st_mtime_ns).get(tier_name)
