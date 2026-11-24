# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the floor-task ticket payload compute: the request, the payload and the verdict."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

KEY_PREFIX = "floor:"

PayloadVerdict = Literal["PAYLOAD", "NEEDS-SPEC", "NO-PARENT", "PLAN-UNREAD"]

# The cause each verdict that is not a payload carries; the candidates compute reads these prefixes.
CAUSE_NEEDS_SPEC = "needs-spec:"
CAUSE_NO_PARENT = "no-parent"
CAUSE_PLAN_UNREAD = "plan-unread"


def floor_key(line_id: str, task_id: str) -> str:
    """The key a floor task is held under: ``floor:<line_id>:<task_id>``."""
    return f"{KEY_PREFIX}{line_id}:{task_id}"


class ModelFloorTaskTicketPayloadRequest(BaseModel):
    """The task to ticket and the text of the plan its line is built from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    # The plan as text; None when the plan could not be read.
    plan_text: str | None


class ModelFloorTaskTicketPayload(BaseModel):
    """The create payload of one floor task's ticket.

    It declares no project, no label and no pillar: the ticket is a Backlog child of the plan
    header's ticket, which is what keeps the old fill's one Linear-ticket rule from admitting it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    floor_key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    parent: str = Field(pattern=r"^OMN-[0-9]+$")


class ModelFloorTaskTicketPayloadResult(BaseModel):
    """The payload of a ready task, or the one verdict that says why none can be built.

    ``missing`` names the parts a ``NEEDS-SPEC`` task lacks; ``cause`` is the text the candidates
    compute reads for a verdict that is not a payload.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: PayloadVerdict
    payload: ModelFloorTaskTicketPayload | None = None
    missing: tuple[str, ...] = ()
    cause: str = ""

    @property
    def ok(self) -> bool:
        """Whether a payload was built."""
        return self.verdict == "PAYLOAD"

    @model_validator(mode="after")
    def _shape_follows_verdict(self) -> ModelFloorTaskTicketPayloadResult:
        if self.verdict == "PAYLOAD":
            if self.payload is None or self.missing or self.cause:
                raise ValueError(
                    "a PAYLOAD carries a payload and no missing part or cause"
                )
            return self
        if self.payload is not None:
            raise ValueError(f"{self.verdict} carries no payload")
        if self.verdict == "NEEDS-SPEC":
            if not self.missing or self.cause != CAUSE_NEEDS_SPEC + ",".join(
                self.missing
            ):
                raise ValueError(
                    "NEEDS-SPEC names its missing parts and a cause that lists them"
                )
        elif self.missing:
            raise ValueError(f"{self.verdict} names no missing part")
        elif self.verdict == "NO-PARENT" and self.cause != CAUSE_NO_PARENT:
            raise ValueError("NO-PARENT carries the cause no-parent")
        elif self.verdict == "PLAN-UNREAD" and self.cause != CAUSE_PLAN_UNREAD:
            raise ValueError("PLAN-UNREAD carries the cause plan-unread")
        return self
