# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide discard or needs-read from patch text and git facts, without I/O."""

from __future__ import annotations

import re
from collections import Counter

from omnimarket.models.rescue_patch_review import (
    EnumRescuePatchVerdict,
    ModelRescuePatchReviewRequest,
    ModelRescuePatchReviewResult,
)

from .handler_junk_paths import is_junk

_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_LOG_FILE = re.compile(r"\.log$")


def changed_files(patch_text: str) -> tuple[str, ...]:
    """Paths named by the ``diff --git`` headers, in order, each once."""
    seen: dict[str, None] = {}
    for line in patch_text.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            seen.setdefault(header.group(2), None)
    return tuple(seen)


def _only_whitespace_changes(patch_text: str) -> bool:
    """True when the removed lines equal the added lines once whitespace is squeezed out."""
    removed: Counter[str] = Counter()
    added: Counter[str] = Counter()
    for line in patch_text.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added["".join(line[1:].split())] += 1
        elif line.startswith("-"):
            removed["".join(line[1:].split())] += 1
    if not removed and not added:
        return False
    return removed == added


class HandlerRescuePatchReview:
    """Decide discard or needs-read for one saved patch."""

    def handle(
        self, request: ModelRescuePatchReviewRequest
    ) -> ModelRescuePatchReviewResult:
        files = changed_files(request.patch_text)
        if not files:
            return ModelRescuePatchReviewResult(
                verdict=EnumRescuePatchVerdict.DISCARD_EMPTY,
                reason="the patch has no changed file",
                files=files,
            )
        if request.already_on_base:
            return ModelRescuePatchReviewResult(
                verdict=EnumRescuePatchVerdict.DISCARD_SUPERSEDED,
                reason="the change is already on the base: the patch reverses cleanly against it",
                files=files,
            )
        if all(is_junk(path) or _LOG_FILE.search(path) for path in files):
            return ModelRescuePatchReviewResult(
                verdict=EnumRescuePatchVerdict.DISCARD_JUNK,
                reason="only build output, logs or caches: " + ", ".join(files),
                files=files,
            )
        if _only_whitespace_changes(request.patch_text):
            return ModelRescuePatchReviewResult(
                verdict=EnumRescuePatchVerdict.DISCARD_FORMATTING,
                reason="every changed line differs from its counterpart by whitespace only",
                files=files,
            )
        state = (
            "applies to the base"
            if request.applies_to_base
            else "no longer applies to the base"
        )
        return ModelRescuePatchReviewResult(
            verdict=EnumRescuePatchVerdict.NEEDS_READ,
            reason=f"a real change that {state}; touches {', '.join(files)}",
            files=files,
        )
