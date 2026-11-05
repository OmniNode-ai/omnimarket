# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Split a document into episodes for Graphiti, which asks callers to chunk long text."""

from __future__ import annotations


def chunk_text(text: str, max_chars: int) -> tuple[str, ...]:
    """Pack whole paragraphs (blank-line separated) into chunks of at most ``max_chars``.

    A paragraph longer than ``max_chars`` is cut at the last space before the limit.
    Every character of every paragraph lands in exactly one chunk, in order.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    pieces: list[str] = []
    for paragraph in (p.strip() for p in text.split("\n\n")):
        while len(paragraph) > max_chars:
            cut = paragraph.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            pieces.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if paragraph:
            pieces.append(paragraph)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        joined = f"{current}\n\n{piece}" if current else piece
        if current and len(joined) > max_chars:
            chunks.append(current)
            current = piece
        else:
            current = joined
    if current:
        chunks.append(current)
    return tuple(chunks)


__all__ = ["chunk_text"]
