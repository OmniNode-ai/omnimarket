# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a request to another owner when that owner's repository already holds the object.

A lane asked an owner to add an entry that the owner's file had held since August: the ask was
drafted from a refusal message and nothing read the file first. The caller reads the file at the
owner's base through the canonical clone,

    git -C "${OMNI_HOME:?}/<repo>" show origin/<base>:<path>

and hands the command, its exit code and its output here. The read counts only in exactly that
form: a checkout, a pin or a fetch of another ref is not the owner's base. Each object is looked
up by its bare name as well as its qualified name, because a file that lists an object by its bare
name under a domain would miss a schema-qualified lookup. The verdict carries the
``verified by: <command> => <observation>`` line the draft must hold; only ABSENT lets the draft go on.
"""

from __future__ import annotations

import re

from omnimarket.models.cross_owner_ask.enum_cross_owner_ask_verdict import (
    EnumCrossOwnerAskVerdict,
)
from omnimarket.models.cross_owner_ask.model_cross_owner_ask_request import (
    ModelCrossOwnerAskRequest,
)
from omnimarket.models.cross_owner_ask.model_cross_owner_ask_result import (
    ModelCrossOwnerAskResult,
)

_MAX_MATCHES_PER_NAME = 5
_MAX_LINE_CHARS = 120


def canonical_read_command(repo: str, base: str, path: str) -> str:
    return f'git -C "${{OMNI_HOME:?}}/{repo}" show origin/{base}:{path}'


def _bare(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _refused(
    verdict: EnumCrossOwnerAskVerdict, command: str, observation: str
) -> ModelCrossOwnerAskResult:
    return ModelCrossOwnerAskResult(
        verdict=verdict,
        draft_allowed=False,
        verified_by=f"verified by: {command} => {observation}",
        terminal_failure_cause=verdict.value,
    )


class HandlerCrossOwnerAskCheck:
    """Pure: runs no command and reads no file; it judges the read the caller made."""

    def handle(self, request: ModelCrossOwnerAskRequest) -> ModelCrossOwnerAskResult:
        command = canonical_read_command(request.repo, request.base, request.path)
        if request.read_command.strip() != command:
            return _refused(
                EnumCrossOwnerAskVerdict.NONCANONICAL_READ,
                command,
                "the read was not made through the canonical clone at the owner's base",
            )
        if request.read_exit_code != 0 or not request.observed_text.strip():
            return _refused(
                EnumCrossOwnerAskVerdict.UNREADABLE,
                command,
                f"exit {request.read_exit_code}, "
                f"{len(request.observed_text)} bytes read; the file was not read",
            )
        lines = request.observed_text.splitlines()
        matches: list[str] = []
        for name in request.names:
            pattern = re.compile(
                rf"(?<![A-Za-z0-9_]){re.escape(_bare(name))}(?![A-Za-z0-9_])"
            )
            hits = [
                f"{name} @ line {number}: {line.strip()[:_MAX_LINE_CHARS]}"
                for number, line in enumerate(lines, start=1)
                if pattern.search(line)
            ]
            matches.extend(hits[:_MAX_MATCHES_PER_NAME])
        names = ", ".join(request.names)
        if matches:
            return ModelCrossOwnerAskResult(
                verdict=EnumCrossOwnerAskVerdict.PRESENT,
                draft_allowed=False,
                verified_by=(
                    f"verified by: {command} | grep -nw -- <bare name of {names}> "
                    f"=> already declared: {matches[0]}"
                ),
                matches=tuple(matches),
            )
        return ModelCrossOwnerAskResult(
            verdict=EnumCrossOwnerAskVerdict.ABSENT,
            draft_allowed=True,
            verified_by=(
                f"verified by: {command} | grep -nw -- <bare name of {names}> "
                f"=> no match in {len(lines)} lines"
            ),
        )
