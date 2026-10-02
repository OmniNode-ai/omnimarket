# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B deterministic source slicing, without effects or model calls."""

from __future__ import annotations

import re
from itertools import pairwise

from omnimarket.models.model_delegation_split_recombine import (
    EnumDelegationSizeBand,
    ModelDelegationSplitRequest,
    ModelDelegationSplitResult,
    ModelDelegationSplitSuccess,
    ModelDelegationSplitUnit,
    ModelNotDecomposable,
)


def _sections(source: str) -> tuple[str, ...]:
    """Split ATX Markdown headings; preserve preamble and fenced code verbatim."""
    boundaries = [0]
    offset = 0
    fence_char = ""
    fence_length = 0
    for line in source.splitlines(keepends=True):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence:
            marker, suffix = fence.groups()
            if not fence_char:
                fence_char, fence_length = marker[0], len(marker)
            elif (
                marker[0] == fence_char
                and len(marker) >= fence_length
                and not suffix.strip()
            ):
                fence_char = ""
        elif not fence_char and re.match(r"^ {0,3}#{1,6}(?:[ \t]+|\r?$)", line):
            if offset:
                boundaries.append(offset)
        offset += len(line)
    boundaries.append(len(source))
    return tuple(
        source[start:end] for start, end in pairwise(boundaries) if start < end
    )


def _review_slices(source: str) -> tuple[str, ...]:
    """Git unified diffs split by file, then by hunk when only one file remains.

    Hunk units retain only the shared file header and their own hunk. A diff
    preamble is retained with the first file, never copied into sibling units.
    Binary and header-only single-file diffs are atomic.
    """
    files = list(re.finditer(r"^diff --git ", source, re.MULTILINE))
    # Also accept ordinary unified diffs without git's diff --git marker.
    if not files:
        files = list(re.finditer(r"^--- [^\n]*\n\+\+\+ ", source, re.MULTILINE))
    if len(files) > 1:
        boundaries = [0, *(match.start() for match in files[1:]), len(source)]
        return tuple(source[start:end] for start, end in pairwise(boundaries))
    hunks = list(
        re.finditer(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", source, re.MULTILINE)
    )
    if len(hunks) < 2:
        return (source,) if source else ()
    header = source[: hunks[0].start()]
    boundaries = [*(match.start() for match in hunks), len(source)]
    return tuple(header + source[start:end] for start, end in pairwise(boundaries))


class HandlerDelegationSplit:
    """Stateless splitter; request fields are the only runtime inputs."""

    def handle(
        self, request: ModelDelegationSplitRequest
    ) -> ModelDelegationSplitResult:
        if request.task_class in {"planning", "reasoning", "code_generation"}:
            return ModelNotDecomposable(
                task_id=request.task_id,
                task_class=request.task_class,
                reason="task_class_not_decomposable",
            )
        bands = tuple(EnumDelegationSizeBand)
        band_index = bands.index(request.size_band)
        if band_index == 0:
            return ModelNotDecomposable(
                task_id=request.task_id,
                task_class=request.task_class,
                reason="minimum_size_band",
            )
        if request.task_class in {"summarization", "document"}:
            slices = _sections(request.source)
        elif request.task_class in {"code_review", "review"}:
            slices = _review_slices(request.source)
        else:
            slices = request.migration_units
        if len(slices) < 2:
            return ModelNotDecomposable(
                task_id=request.task_id,
                task_class=request.task_class,
                reason="no_split_boundary",
            )
        return ModelDelegationSplitSuccess(
            task_id=request.task_id,
            units=tuple(
                ModelDelegationSplitUnit(
                    unit_id=f"{request.task_id}:{position}",
                    position=position,
                    task_class=request.task_class,
                    size_band=bands[band_index - 1],
                    source=source,
                )
                for position, source in enumerate(slices)
            ),
        )
