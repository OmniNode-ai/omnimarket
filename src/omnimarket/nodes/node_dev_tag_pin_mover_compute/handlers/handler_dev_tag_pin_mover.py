# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDevTagPinMover - move a consumer's uv git pin to a new dev tag (OMN-19998).

Operator rulings 2026-10-16T07:34:18Z and 09:35:49Z: internal consumers read a
sibling at its dev tag, never wait on a release, and the move is automatic.
Every dev merge of a sibling is tagged ``dev-<UTC 14 digits>-<sha12>``. This
handler is the pure half of the move: given a consumer's ``pyproject.toml``
text, the sibling's distribution and repository, and the new tag, it rewrites
the ``[tool.uv.sources]`` entry ``<dist> = { git = "<url>", tag = "<old>" }``
to the new tag.

The caller locks and tests the consumer on ``candidate_text`` and calls again
with the outcome; only a call whose ``consumer_tests`` is ``passed`` is accepted,
so a consumer whose tests fail on the new tag keeps its old pin and no PR opens.
It also refuses a tag that is not a dev tag, a consumer that does not pin the
sibling by git tag, and a tag that is not strictly newer than the current one.
"""

from __future__ import annotations

import re
from typing import Literal

from omnimarket.nodes.node_dev_tag_pin_mover_compute.models.model_dev_tag_pin_mover import (
    EnumConsumerTestOutcome,
    EnumPinMoveRefusal,
    ModelDevTagPinMoveRequest,
    ModelDevTagPinMoveResult,
)

_DEV_TAG = re.compile(r"^dev-(?P<stamp>\d{14})-[0-9a-f]{12}$")


def _source_line(distribution: str, repository_url: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?m)^(?P<head>{re.escape(distribution)}[ \t]*=[ \t]*\{{[ \t]*"
        rf'git[ \t]*=[ \t]*"{re.escape(repository_url)}"[ \t]*,[ \t]*'
        rf'tag[ \t]*=[ \t]*")(?P<tag>[^"]+)(?P<tail>"[ \t]*\}})'
    )


def _refuse(
    refusal: EnumPinMoveRefusal,
    detail: str,
    *,
    current_tag: str | None = None,
    new_tag: str | None = None,
    candidate_text: str = "",
) -> ModelDevTagPinMoveResult:
    return ModelDevTagPinMoveResult(
        accepted=False,
        refusal=refusal,
        detail=detail,
        current_tag=current_tag,
        new_tag=new_tag,
        candidate_text=candidate_text,
    )


class HandlerDevTagPinMover:
    """COMPUTE handler: rewrite the consumer's dev-tag pin, or refuse."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    def handle(self, request: ModelDevTagPinMoveRequest) -> ModelDevTagPinMoveResult:
        new_tag = request.new_tag.strip()
        new_match = _DEV_TAG.match(new_tag)
        if new_match is None:
            return _refuse(
                EnumPinMoveRefusal.NOT_A_DEV_TAG,
                f"{request.new_tag!r} is not a dev-<UTC14>-<sha12> tag",
            )

        pattern = _source_line(request.distribution, request.repository_url)
        pins = [m.group("tag") for m in pattern.finditer(request.pyproject_text)]
        if len(pins) != 1:
            return _refuse(
                EnumPinMoveRefusal.NO_TAG_PIN,
                f"expected one {request.distribution} git source at "
                f"{request.repository_url} pinned by tag, found {len(pins)}",
                new_tag=new_tag,
            )
        current_tag = pins[0]
        current_match = _DEV_TAG.match(current_tag)
        if current_match is not None and new_match.group(
            "stamp"
        ) <= current_match.group("stamp"):
            return _refuse(
                EnumPinMoveRefusal.NOT_FORWARD,
                f"{new_tag} is not newer than the current pin {current_tag}",
                current_tag=current_tag,
                new_tag=new_tag,
            )

        candidate = pattern.sub(
            lambda m: f"{m.group('head')}{new_tag}{m.group('tail')}",
            request.pyproject_text,
        )
        if request.consumer_tests is EnumConsumerTestOutcome.NOT_RUN:
            return _refuse(
                EnumPinMoveRefusal.CONSUMER_TESTS_NOT_RUN,
                "lock and test the consumer on candidate_text, then call again "
                "with the outcome",
                current_tag=current_tag,
                new_tag=new_tag,
                candidate_text=candidate,
            )
        if request.consumer_tests is EnumConsumerTestOutcome.FAILED:
            return _refuse(
                EnumPinMoveRefusal.CONSUMER_TESTS_FAILED,
                f"the consumer's tests fail on {new_tag}; the pin stays at "
                f"{current_tag}",
                current_tag=current_tag,
                new_tag=new_tag,
            )
        return ModelDevTagPinMoveResult(
            accepted=True,
            current_tag=current_tag,
            new_tag=new_tag,
            candidate_text=candidate,
        )


__all__ = ["HandlerDevTagPinMover"]
