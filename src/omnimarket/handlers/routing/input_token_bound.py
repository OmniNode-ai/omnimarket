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
each, a word that holds a digit (``a1b2c3d``, UUIDs, hashes, base64, numbered
identifiers) counts one token per byte, a non-ASCII byte counts one token,
whitespace counts one token per character, a run of ASCII letters that is not
a plain word counts one token per letter (a run of 13 or more letters, a run of
two or more upper-case letters, or a run that switches case twice or more,
overlapping switches counted, as random mixed-case text does), and only the
remaining lower-case or capitalised letter runs of up to 12 letters are
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
    "LOCAL_MEMBER_OUTPUT_RESERVES",
    "LOCAL_MEMBER_WINDOWS",
    "LOCAL_TIER_NAME",
    "WRAPPER_MARGIN_TOKENS",
    "clamp_max_tokens",
    "input_bound_members",
    "input_token_upper_bound",
    "member_input_bound",
]

LOCAL_TIER_NAME = "local"
# What each local member serves, read from the endpoint it names (2026-11-17):
# the rungs' vLLM reports max_model_len 131072; the spread peer's llama.cpp
# reports one 131072-token KV pool over two slots, so a slot is guaranteed
# 65536 tokens. The output reserve is the backend's own max_tokens (rungs 65536,
# peer 16384), and the wrapper is 221 to 429 metered tokens over the 19 local
# classes (fixtures/metered_token_readings.json), inside a 1024-token margin.
WRAPPER_MARGIN_TOKENS = 1024
LOCAL_MEMBER_WINDOWS: Mapping[str, int] = {
    "local-coder": 131072,
    "local-heavy-reasoning": 131072,
    "local-omnipc2-chat": 65536,
}
LOCAL_MEMBER_OUTPUT_RESERVES: Mapping[str, int] = {
    "local-coder": 65536,
    "local-heavy-reasoning": 65536,
    "local-omnipc2-chat": 16384,
}
# 131072 - 65536 - 1024 = 64512 for each rung; 65536 - 16384 - 1024 = 48128 for
# the peer.
LOCAL_MEMBER_INPUT_BOUNDS: Mapping[str, int] = {
    ref: LOCAL_MEMBER_WINDOWS[ref]
    - LOCAL_MEMBER_OUTPUT_RESERVES[ref]
    - WRAPPER_MARGIN_TOKENS
    for ref in LOCAL_MEMBER_WINDOWS
}
DEFAULT_LOCAL_MEMBER_INPUT_BOUND = min(LOCAL_MEMBER_INPUT_BOUNDS.values())
_DEFAULT_LOCAL_MEMBER_WINDOW = min(LOCAL_MEMBER_WINDOWS.values())

# A whitespace-delimited word that holds a digit is treated as an identifier,
# hash or encoded blob: one token per byte, whatever its length (``a1b2c3d ``
# meters 7.5 tokens per copy on the lab endpoint).
# A run of ASCII letters longer than a natural word is counted per letter.
_LETTER_RUN_MAX_NATURAL = 12
_LETTER_TOKENS = 0.5
_WORD = re.compile(r"\S+")
_LETTER_RUN = re.compile(r"[A-Za-z]+")
# Every position that starts a case switch, overlapping ones included (``aBc``
# holds two: ``aB`` and ``Bc``); a plain ``findall`` of the pair would count one.
_CASE_SWITCH = re.compile(r"(?=[a-z][A-Z]|[A-Z][a-z])")


def _letter_run_tokens(run: str) -> float:
    """Tokens an ASCII letter run can meter: per letter unless it is a plain word."""
    if len(run) > _LETTER_RUN_MAX_NATURAL:
        return float(len(run))
    # Upper-case runs (``ABCDEFGHIJKL``) meter above two letters per token.
    if len(run) >= 2 and run.isupper():
        return float(len(run))
    # ``Word`` has one upper-to-lower switch; ``aBc`` has two, ``aBcD`` has three.
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
        if any(c.isdigit() for c in word):
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


def member_input_bound(
    tier_name: str, backend_ref: str, backend_max_tokens: int | None = None
) -> int | None:
    """The input-token bound a member of ``tier_name`` can take, or ``None``.

    Only the local tier is bounded: its members are served windows the router
    does not meter. A local member that declares no window takes the smallest
    declared one (fail closed); a member of another tier has no bound here.
    ``backend_max_tokens`` is the resolved backend's own output reserve; without
    it the member's declared reserve is used.
    """
    if tier_name != LOCAL_TIER_NAME:
        return None
    window = LOCAL_MEMBER_WINDOWS.get(backend_ref, _DEFAULT_LOCAL_MEMBER_WINDOW)
    if backend_max_tokens is None:
        if backend_ref in LOCAL_MEMBER_INPUT_BOUNDS:
            return LOCAL_MEMBER_INPUT_BOUNDS[backend_ref]
        return DEFAULT_LOCAL_MEMBER_INPUT_BOUND
    return window - backend_max_tokens - WRAPPER_MARGIN_TOKENS


def clamp_max_tokens(
    prompt: str,
    tier_name: str,
    backend_ref: str,
    max_tokens: int,
) -> int:
    """``max_tokens`` cut so the prompt's upper bound plus it fits the window.

    Applied at dispatch to the resolved backend (its tier, id and ``max_tokens``),
    so a prompt the counting undercounts still cannot take the endpoint past its
    window: ``window - wrapper margin - input bound`` is the most output it may
    reserve. A tier other than the local one is returned unchanged. A prompt that
    leaves no room for one output token is refused with ``ValueError`` rather
    than sent with ``max_tokens`` 1.
    """
    if tier_name != LOCAL_TIER_NAME:
        return max_tokens
    window = LOCAL_MEMBER_WINDOWS.get(backend_ref, _DEFAULT_LOCAL_MEMBER_WINDOW)
    room = window - WRAPPER_MARGIN_TOKENS - input_token_upper_bound(prompt)
    if room < 1:
        raise ValueError(
            f"prompt input bound leaves no output room on {backend_ref}: "
            f"window {window}, margin {WRAPPER_MARGIN_TOKENS}, room {room}"
        )
    return min(max_tokens, room)


class _TierMember(Protocol):
    @property
    def backend_ref(self) -> str: ...


def input_bound_members[M: _TierMember](
    tier_name: str, models: Sequence[M], prompt: str
) -> tuple[M, ...]:
    """The members of the tier whose input bound holds the prompt's upper bound.

    ``models`` are tier models (anything with a ``backend_ref``). ``prompt`` is
    required: a caller with no prompt of its own (an availability diagnostic)
    passes ``""`` and keeps every member, a caller that has one must pass it.
    """
    if tier_name != LOCAL_TIER_NAME:
        return tuple(models)
    bound = input_token_upper_bound(prompt)
    return tuple(
        model
        for model in models
        if bound <= (member_input_bound(tier_name, model.backend_ref) or 0)
    )
