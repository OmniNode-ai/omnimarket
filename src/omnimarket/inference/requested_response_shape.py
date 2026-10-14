# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Resolve the response shape a delegation prompt declared for itself (OMN-16932).

A task class's definition-of-done describes the class. ``research`` cites
sources and reasons methodically because research answers do. That rubric is
correct for a research *question* and a category error for a research-classed
request that says "Reply with exactly the word: alive" — the model obeys, the
gate scores the obedience 0.7 against a prose rubric, rejects it on the free
local rung three times, and climbs a metered ladder looking for a longer wrong
answer (dev lane, 2026-08-30, five attempts, two 429s, terminal failed).

This module is the pure, contract-fed resolver for the request-scoped half of
that expectation. The directives are DECLARED in
``task_class_contracts.v1.yaml`` (``response_shape_directives``) and passed in
— nothing here is hardcoded, and the caller that owns the contract read owns
the authority.
"""

from __future__ import annotations

import re
from functools import lru_cache

from omnimarket.enums.enum_requested_response_shape import EnumRequestedResponseShape

# Declaration order = match precedence. EXACT_LITERAL is strictly more specific
# than SINGLE_WORD ("reply with exactly the word: alive" also reads as a
# one-word ask), so it is tested first and a prompt can only resolve to one
# shape.
_SHAPE_PRECEDENCE: tuple[EnumRequestedResponseShape, ...] = (
    EnumRequestedResponseShape.EXACT_LITERAL,
    EnumRequestedResponseShape.SINGLE_WORD,
    EnumRequestedResponseShape.LENGTH_CONSTRAINED,
)

# A directive is a self-description of the ANSWER, so only the instruction
# framing counts. Matching the whole prompt would let a research question that
# merely discusses one-word answers resolve as constrained; capping the scanned
# window keeps the directive a property of the ask.
#
# The framing sits at either end of a prompt: a short ask leads with it, and a
# task that carries a program or a document states its output requirement last
# (OMN-20844: "Reply with only the Python literal ..." closes a prompt whose
# first 400 characters are code). So the window is the head and the tail, joined
# by a newline; a prompt no longer than the window is scanned whole, exactly as
# before. The join keeps `^`/`$` anchored patterns anchored to the whole prompt:
# a longer prompt's window starts with its head and ends with its tail, so an
# anchored directive can only match when the prompt IS that directive.
_DIRECTIVE_SCAN_CHARS = 400


def _directive_window(prompt: str) -> str:
    if len(prompt) <= 2 * _DIRECTIVE_SCAN_CHARS:
        return prompt
    return f"{prompt[:_DIRECTIVE_SCAN_CHARS]}\n{prompt[-_DIRECTIVE_SCAN_CHARS:]}"


@lru_cache(maxsize=64)
def _compile(pattern: str) -> re.Pattern[str]:
    """Compile one declared directive pattern (case-insensitive)."""
    return re.compile(pattern, re.IGNORECASE)


def resolve_requested_response_shape(
    prompt: str,
    directives: dict[EnumRequestedResponseShape, tuple[str, ...]],
) -> EnumRequestedResponseShape:
    """Return the response shape ``prompt`` declares for itself.

    Args:
        prompt: the caller's prompt text.
        directives: the contract-declared patterns, keyed by the shape they
            declare. An empty mapping — the contract declares no directives, or
            the contract file is absent — always resolves ``UNCONSTRAINED``, so
            a deployment that has not adopted the declaration keeps the exact
            prior behaviour rather than silently acquiring a new one.

    Returns:
        The single most specific declared shape that matched, or
        ``UNCONSTRAINED`` when none did.
    """
    window = _directive_window(prompt)
    for shape in _SHAPE_PRECEDENCE:
        for pattern in directives.get(shape, ()):
            if _compile(pattern).search(window):
                return shape
    return EnumRequestedResponseShape.UNCONSTRAINED


__all__: list[str] = ["resolve_requested_response_shape"]
