# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOmniclaudePinBump - move the omniclaude pin to a released version (OMN-20001).

Operator ruling 2026-10-01T21:43:10Z (3): a producer release that extends a
cross-repo contract notifies its consumer, and the consumer's update is a PR
that lands through the consumer's own checks. This handler is the pure half of
that PR: given the text of ``ci.yml`` and the version omniclaude released, it
returns the text with every omniclaude pin moved to that version's tag.

It refuses, and the workflow opens no PR, when the version is not a plain
release (a release candidate or a branch name), when the text names no pin, when
a read of omniclaude is not at a release tag, when the pins disagree, and when
the new version is not strictly ahead of the current one.
"""

from __future__ import annotations

import re
from typing import Literal

from omnimarket.nodes.node_omniclaude_pin_bump_compute.models.model_omniclaude_pin_bump import (
    EnumPinBumpRefusal,
    ModelOmniclaudePinBumpRequest,
    ModelOmniclaudePinBumpResult,
)

_VERSION = re.compile(r"^v?(?P<v>\d+\.\d+\.\d+)$")
_TAG = re.compile(r"^v\d+\.\d+\.\d+$")
# The two places ci.yml reads omniclaude: a shallow clone at --branch, and a
# checkout step whose ``ref:`` follows ``repository: OmniNode-ai/omniclaude``.
_CLONE = re.compile(
    r"(?P<head>--branch )(?P<ref>\S+)"
    r"(?P<tail> https://github\.com/OmniNode-ai/omniclaude\.git)"
)
_CHECKOUT = re.compile(
    r"(?P<head>repository: OmniNode-ai/omniclaude\n(?:[ \t]+[a-z-]+: [^\n]*\n)*?"
    r"[ \t]+ref: )(?P<ref>\S+)"
)


def _as_tuple(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in tag.removeprefix("v").split("."))


def _refuse(
    refusal: EnumPinBumpRefusal,
    detail: str,
    *,
    current_tag: str | None = None,
    new_tag: str | None = None,
) -> ModelOmniclaudePinBumpResult:
    return ModelOmniclaudePinBumpResult(
        accepted=False,
        refusal=refusal,
        detail=detail,
        current_tag=current_tag,
        new_tag=new_tag,
    )


class HandlerOmniclaudePinBump:
    """COMPUTE handler: rewrite every omniclaude pin, or refuse."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    def handle(
        self, request: ModelOmniclaudePinBumpRequest
    ) -> ModelOmniclaudePinBumpResult:
        match = _VERSION.match(request.new_version.strip())
        if match is None:
            return _refuse(
                EnumPinBumpRefusal.NOT_A_RELEASE_VERSION,
                f"{request.new_version!r} is not a plain X.Y.Z release version",
            )
        new_tag = f"v{match.group('v')}"

        refs = [m.group("ref") for m in _CLONE.finditer(request.ci_text)]
        refs += [m.group("ref") for m in _CHECKOUT.finditer(request.ci_text)]
        if not refs:
            return _refuse(
                EnumPinBumpRefusal.NO_PIN_FOUND,
                "no read of omniclaude found in the workflow text",
                new_tag=new_tag,
            )
        loose = sorted({ref for ref in refs if _TAG.match(ref) is None})
        if loose:
            return _refuse(
                EnumPinBumpRefusal.PIN_NOT_A_RELEASE_TAG,
                f"omniclaude is read at {loose}, not at a release tag",
                new_tag=new_tag,
            )
        distinct = sorted(set(refs), key=_as_tuple)
        if len(distinct) > 1:
            return _refuse(
                EnumPinBumpRefusal.PINS_DISAGREE,
                f"omniclaude is pinned at {distinct}; one pin is the invariant",
                new_tag=new_tag,
            )
        current_tag = distinct[0]
        if _as_tuple(new_tag) <= _as_tuple(current_tag):
            return _refuse(
                EnumPinBumpRefusal.NOT_FORWARD,
                f"{new_tag} is not ahead of the current pin {current_tag}",
                current_tag=current_tag,
                new_tag=new_tag,
            )

        def move(m: re.Match[str]) -> str:
            return f"{m.group('head')}{new_tag}{m.groupdict().get('tail', '')}"

        updated = _CLONE.sub(move, request.ci_text)
        updated = _CHECKOUT.sub(move, updated)
        return ModelOmniclaudePinBumpResult(
            accepted=True,
            current_tag=current_tag,
            new_tag=new_tag,
            updated_text=updated,
        )


__all__ = ["HandlerOmniclaudePinBump"]
