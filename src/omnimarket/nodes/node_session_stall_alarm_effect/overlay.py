# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_session_stall_alarm_effect: where the transcript is and what counts as a stall (OMN-25405).

Found at ``<root>/node_session_stall_alarm_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``.
The package ships ``overlay.example.yaml`` (synthetic values). Nothing here has a default: an absent key refuses
the load and names it.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_session_stall_alarm_effect"


class ModelSessionStallOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: What the alarm is about; one episode per subject.
    subject: str = Field(min_length=1)
    #: The session transcripts: a file glob, of which the newest file by modification time is read.
    transcript_glob: str = Field(min_length=1)
    #: Read at most this many bytes from the end of the file (a transcript grows to hundreds of megabytes).
    tail_bytes: int = Field(ge=1)
    #: Rows older than this many seconds before now are not judged.
    window_seconds: int = Field(ge=1)
    #: A recurring session tick is declared: a quiet scheduler is a stall only then.
    tick_declared: bool
    dequeue_after_seconds: int = Field(ge=1)
    task_after_seconds: int = Field(ge=1)
    #: An enqueue whose content holds one of these also counts as a scheduled task firing.
    task_markers: tuple[str, ...] = Field(min_length=1)


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelSessionStallOverlay:
    return load_structured_overlay(NODE_NAME, ModelSessionStallOverlay, environ)


__all__ = ["NODE_NAME", "ModelSessionStallOverlay", "load_overlay"]
