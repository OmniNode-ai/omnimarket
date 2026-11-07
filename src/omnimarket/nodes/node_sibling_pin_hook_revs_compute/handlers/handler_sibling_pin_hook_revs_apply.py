# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Apply the sibling-pin hook revs plan to a pre-commit config text (OMN-22753).

``HandlerSiblingPinHookRevs`` says which tag the floor-bound hook blocks and which tag the
source pin take. This handler reads the hook blocks of the pin's repository out of the
consumer's ``.pre-commit-config.yaml`` text, in file order, asks that plan, and returns the
text with the planned blocks moved to the plan's hook tag. Nothing else in the text changes:
a trailing comment on a ``rev:`` line, quotes and every other block stay as written.

Pure: the command line reads and writes the file, the train writes the pin.
"""

from __future__ import annotations

import re

from ..models import (
    ModelSiblingPinHookRevsApplyRequest,
    ModelSiblingPinHookRevsApplyResult,
    ModelSiblingPinHookRevsRequest,
)
from .handler_sibling_pin_hook_revs import HandlerSiblingPinHookRevs

_REPO_LINE = re.compile(r"^\s*-\s+repo:\s*(?P<url>\S+)\s*$")
_REV_LINE = re.compile(
    r"^(?P<head>\s+rev:\s*)(?P<quote>[\"']?)(?P<rev>[^\s\"'#]+)(?P=quote)(?P<tail>.*)$"
)


def _same_repository(url: str, repository_url: str) -> bool:
    def bare(value: str) -> str:
        return value.rstrip("/").removesuffix(".git")

    return bare(url) == bare(repository_url)


class HandlerSiblingPinHookRevsApply:
    """Plan, then rewrite the planned hook blocks; never touch a file."""

    def handle(
        self, request: ModelSiblingPinHookRevsApplyRequest
    ) -> ModelSiblingPinHookRevsApplyResult:
        lines = request.precommit_text.splitlines(keepends=True)
        blocks: list[tuple[int, re.Match[str]]] = []
        in_block = False
        for number, line in enumerate(lines):
            repo = _REPO_LINE.match(line)
            if repo is not None:
                in_block = _same_repository(repo.group("url"), request.repository_url)
            elif in_block and (rev := _REV_LINE.match(line.rstrip("\r\n"))):
                blocks.append((number, rev))
                in_block = False
        revs = tuple(rev.group("rev") for _, rev in blocks)
        plan = HandlerSiblingPinHookRevs().handle(
            ModelSiblingPinHookRevsRequest(
                pin_tag=request.pin_tag,
                hook_revs=revs,
                candidates=request.candidates,
                tag_pattern=request.tag_pattern,
            )
        )
        if plan.hook_tag is not None:
            for index in plan.hook_indexes:
                number, rev = blocks[index]
                quote = rev.group("quote")
                ending = lines[number][len(lines[number].rstrip("\r\n")) :]
                lines[number] = (
                    f"{rev.group('head')}{quote}{plan.hook_tag}{quote}"
                    f"{rev.group('tail')}{ending}"
                )
        return ModelSiblingPinHookRevsApplyResult(
            plan=plan, hook_revs=revs, candidate_text="".join(lines)
        )
