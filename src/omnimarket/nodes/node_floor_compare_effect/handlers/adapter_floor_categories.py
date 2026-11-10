# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The category roll-up and the details rows of the compare page (OMN-23018).

Pure functions: lines in, cards and rows out. Which key belongs to which category is the overlay's
(``ModelFloorCompareOverlay``); nothing here names a key. A line no prefix claims goes to the overlay's ``other`` so the
roll-up always accounts for every line of the window.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelCategoryCard,
    ModelCompareCategory,
    ModelCompareLine,
    ModelDetailRow,
    ModelFloorCompareOverlay,
)

COLOUR_UNIT = "colour"


def _numbers(n: int) -> str:
    return "1 number" if n == 1 else f"{n} numbers"


def category_of(key: str, overlay: ModelFloorCompareOverlay) -> ModelCompareCategory:
    """The category whose prefix is the longest one ``key`` starts with; ``other`` when none does."""
    best: tuple[int, ModelCompareCategory] | None = None
    for category in overlay.categories:
        for prefix in category.prefixes:
            if key.startswith(prefix) and (best is None or len(prefix) > best[0]):
                best = (len(prefix), category)
    return best[1] if best else overlay.other


def size_of(line: ModelCompareLine) -> float | None:
    """The relative size of the move: absolute change over the old value (at least 1); ``None`` when a side is unread."""
    if line.old is None or line.new is None:
        return None
    return abs(line.new - line.old) / max(abs(line.old), 1.0)


def change_text(line: ModelCompareLine) -> str:
    if line.old is None or line.new is None:
        return "no reading"
    if line.old == line.new:
        return "no change"
    if line.unit == COLOUR_UNIT:
        return f"{line.old_text} to {line.new_text}"
    delta = line.new - line.old
    share = abs(delta) / abs(line.old) if line.old else None
    tail = f" ({share:.0%})" if share is not None else ""
    return f"{'up' if delta > 0 else 'down'} {abs(delta):g}{tail}"


def _move_sentence(line: ModelCompareLine) -> str:
    """One plain clause of a move: ``In-scope merges up from 9 to 14 (merges in the last hour), which is better``."""
    if line.old is None or line.new is None:
        raise ValueError("a move needs a reading on both sides")
    if line.unit == COLOUR_UNIT:
        move = f"{line.name} went from {line.old_text} to {line.new_text}"
    else:
        way = "up" if line.new > line.old else "down"
        move = f"{line.name} {way} from {line.old:g} to {line.new:g} ({line.unit})"
    return f"{move}, which is {line.verdict}"


def _card(
    category: ModelCompareCategory,
    lines: Sequence[ModelCompareLine],
    window_label: str,
) -> ModelCategoryCard:
    count = {
        v: sum(1 for ln in lines if ln.verdict == v)
        for v in ("better", "worse", "same", "unread")
    }
    moved = [ln for ln in lines if ln.verdict in ("better", "worse")]
    biggest = max(moved, key=lambda ln: (size_of(ln) or 0.0, ln.key)) if moved else None
    total = len(lines)
    if total == 0:
        lean = "empty"
        biggest_change = "No numbers are read for this category yet."
    elif biggest is not None:
        lean = (
            "mixed"
            if count["better"] == count["worse"]
            else "better"
            if count["better"] > count["worse"]
            else "worse"
        )
        biggest_change = f"Biggest change: {_move_sentence(biggest)}."
    elif count["unread"] == total:
        lean = "unread"
        biggest_change = f"No reading from {window_label} ago for any of its {_numbers(total)}, so nothing is compared."
    else:
        lean = "same"
        biggest_change = "Nothing moved."
        if count["unread"]:
            biggest_change += (
                f" {_numbers(count['unread'])} with no reading from {window_label} ago."
            )
    return ModelCategoryCard(
        id=category.id,
        title=category.title,
        better=count["better"],
        worse=count["worse"],
        same=count["same"],
        unread=count["unread"],
        total=total,
        lean=lean,
        sentence=f"{category.title}: {count['better']} better, {count['worse']} worse; {biggest_change[0].lower()}{biggest_change[1:]}",
        biggest_change=biggest_change,
        biggest_key=biggest.key if biggest else None,
    )


def rollup(
    lines: Sequence[ModelCompareLine],
    overlay: ModelFloorCompareOverlay,
    window_label: str,
) -> tuple[ModelCategoryCard, ...]:
    """One card per declared category, in the overlay's order, plus ``other`` when a line fell to it. Counts use every line."""
    cards = [
        _card(c, [ln for ln in lines if ln.category == c.id], window_label)
        for c in overlay.categories
    ]
    stray = [ln for ln in lines if ln.category == overlay.other.id]
    if stray:
        cards.append(_card(overlay.other, stray, window_label))
    return tuple(cards)


def detail_rows(lines: Sequence[ModelCompareLine]) -> tuple[ModelDetailRow, ...]:
    """Every line as a short row, the biggest move first, lines without a reading last."""
    rows = [
        ModelDetailRow(
            key=ln.key,
            name=ln.name,
            category=ln.category,
            verdict=ln.verdict,
            old_text=ln.old_text,
            new_text=ln.new_text,
            change_text=change_text(ln),
            size=size_of(ln),
            better_is=ln.better_is,
        )
        for ln in lines
    ]
    rows.sort(key=lambda r: (r.size is None, -(r.size or 0.0), r.key))
    return tuple(rows)
