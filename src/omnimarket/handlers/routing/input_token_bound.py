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
one token, whitespace counts one token per character, a run of ASCII letters
that is not a plain word counts one token per letter (a run of 13 or more
letters, or a run that switches case twice or more, as random mixed-case text
does), and only the remaining short single-case or capitalised letter runs are
discounted, at two per token.

Each local member declares the bound it can take (``LOCAL_MEMBER_INPUT_BOUNDS``,
keyed by backend id) and :func:`input_bound_members` drops a member whose bound
is below the prompt's, on every selection path: the reducer, the same-tier
sibling probe, ``first_eligible_tier``, ``next_eligible_tier`` and
``backend_id_for_tier``. A member of the local tier that declares no bound takes
the smallest declared one, so an unlisted member (a spread peer added later, a
ladder mounted without this table) fails closed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Protocol

__all__ = [
    "DEFAULT_LOCAL_MEMBER_INPUT_BOUND",
    "LOCAL_MEMBER_INPUT_BOUNDS",
    "LOCAL_TIER_NAME",
    "input_bound_members",
    "input_token_upper_bound",
    "member_input_bound",
]

LOCAL_TIER_NAME = "local"
# Served window 131072, output reserve 65536 (the backends' max_tokens) and a
# 1024-token wrapper margin: 131072 - 65536 - 1024 = 64512 for each rung.
# The spread peer holds 65536 tokens per slot and reserves 16384 for output:
# 65536 - 16384 - 1024 = 48128. The wrapper is 221 to 429 metered tokens over
# the 19 local classes (fixtures/metered_token_readings.json).
LOCAL_MEMBER_INPUT_BOUNDS: Mapping[str, int] = {
    "local-coder": 64512,
    "local-heavy-reasoning": 64512,
    "local-omnipc2-chat": 48128,
}
DEFAULT_LOCAL_MEMBER_INPUT_BOUND = min(LOCAL_MEMBER_INPUT_BOUNDS.values())

# A whitespace-delimited word at least this long that holds a digit is treated
# as an identifier, hash or encoded blob: one token per byte.
_DENSE_WORD_MIN_LENGTH = 16
# A run of ASCII letters longer than a natural word is counted per letter.
_LETTER_RUN_MAX_NATURAL = 12
_LETTER_TOKENS = 0.5
_WORD = re.compile(r"\S+")
_LETTER_RUN = re.compile(r"[A-Za-z]+")
_CASE_SWITCH = re.compile(r"[a-z][A-Z]|[A-Z][a-z]")


def _letter_run_tokens(run: str) -> float:
    """Tokens an ASCII letter run can meter: per letter unless it is a plain word."""
    if len(run) > _LETTER_RUN_MAX_NATURAL:
        return float(len(run))
    # ``Word`` has one upper-to-lower switch; ``aBcD`` has three.
    if len(_CASE_SWITCH.findall(run)) >= 2:
        return float(len(run))
    return len(run) * _LETTER_TOKENS


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
        position = 0
        for run in _LETTER_RUN.finditer(word):
            total += _other_tokens(word[position : run.start()])
            total += _letter_run_tokens(run.group())
            position = run.end()
        total += _other_tokens(word[position:])
    total += len(text) - last
    return math.ceil(total)


def _other_tokens(chars: str) -> int:
    """Tokens of characters that are not ASCII letters: one per byte or per char."""
    return sum(len(c.encode()) if ord(c) > 127 else 1 for c in chars)


def member_input_bound(tier_name: str, backend_ref: str) -> int | None:
    """The input-token bound a member of ``tier_name`` can take, or ``None``.

    Only the local tier is bounded: its members are served windows the router
    does not meter. A local member that declares no bound takes the smallest
    declared one (fail closed); a member of another tier has no bound here.
    """
    if tier_name != LOCAL_TIER_NAME:
        return None
    return LOCAL_MEMBER_INPUT_BOUNDS.get(backend_ref, DEFAULT_LOCAL_MEMBER_INPUT_BOUND)


class _TierMember(Protocol):
    @property
    def backend_ref(self) -> str: ...


def input_bound_members[M: _TierMember](
    tier_name: str, models: Sequence[M], prompt: str
) -> tuple[M, ...]:
    """The members of the tier whose input bound holds the prompt's upper bound.

    ``models`` are tier models (anything with a ``backend_ref``). An empty
    prompt (an availability probe that names no prompt) keeps every member.
    """
    if not prompt or tier_name != LOCAL_TIER_NAME:
        return tuple(models)
    bound = input_token_upper_bound(prompt)
    return tuple(
        model
        for model in models
        if bound
        <= LOCAL_MEMBER_INPUT_BOUNDS.get(
            model.backend_ref, DEFAULT_LOCAL_MEMBER_INPUT_BOUND
        )
    )
