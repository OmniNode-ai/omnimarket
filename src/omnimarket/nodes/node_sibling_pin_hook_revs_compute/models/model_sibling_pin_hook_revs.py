# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and plan of the sibling-pin hook revs node (OMN-22641)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumHookRevsReason(StrEnum):
    """Why the plan is what it is."""

    HOOKS_AND_PIN_MOVED = "hooks_and_pin_moved"
    PIN_MOVED_NO_BOUND_HOOKS = "pin_moved_no_bound_hooks"
    PIN_MOVED_HOOKS_ALREADY_AHEAD = "pin_moved_hooks_already_ahead"
    NO_NEWER_GREEN_TAG = "no_newer_green_tag"
    NO_GREEN_TAG_UNDER_HOOKS = "no_green_tag_under_hooks"


class ModelHookRevsCandidate(BaseModel):
    """One dev tag of the pin's repository, as the night's read of its dev CI left it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: str = Field(description="The dev merge tag, dev-<UTC14>-<sha12>")
    green: bool | None = Field(
        default=None,
        description="True when dev CI on the tag's commit is green; False red; None unread (never chosen)",
    )


class ModelSiblingPinHookRevsRequest(BaseModel):
    """The source pin, the hook revs of the pin's repository and the dev tags newer than the pin."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pin_tag: str = Field(description="The dev merge tag of the consumer's source pin")
    hook_revs: tuple[str, ...] = Field(
        description="The rev of every hook block of the pin's repository, in file order; a rev that is not a dev merge tag is ignored"
    )
    candidates: tuple[ModelHookRevsCandidate, ...] = Field(
        description="Dev tags of the pin's repository, newest first; tags not newer than the pin end the read"
    )
    tag_pattern: str = Field(
        default=r"^dev-(?P<stamp>[0-9]{14})-[0-9a-f]{12}$",
        description="The dev merge tag grammar; the group named stamp orders tags (the internal sibling pins contract's dev_tag_pattern)",
    )


class ModelSiblingPinHookRevsPlan(BaseModel):
    """What the train writes: a tag for the bound hook blocks, a tag for the source pin."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hook_tag: str | None = Field(
        description="The tag the floor-bound hook blocks move to; None when none moves"
    )
    hook_indexes: tuple[int, ...] = Field(
        description="Indexes into hook_revs of the blocks that move to hook_tag"
    )
    pin_tag: str | None = Field(
        description="The tag the source pin moves to; None leaves the pin where it was"
    )
    reason: EnumHookRevsReason


class ModelSiblingPinHookRevsApplyRequest(BaseModel):
    """A consumer's pre-commit config text and the same facts the plan reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    precommit_text: str = Field(
        description="The consumer's .pre-commit-config.yaml text"
    )
    repository_url: str = Field(
        description="The pin's repository url; a hook block of this repository, with or without .git, is read"
    )
    pin_tag: str = Field(description="The dev merge tag of the consumer's source pin")
    candidates: tuple[ModelHookRevsCandidate, ...] = Field(
        description="Dev tags of the pin's repository, newest first; tags not newer than the pin end the read"
    )
    tag_pattern: str = Field(
        default=ModelSiblingPinHookRevsRequest.model_fields["tag_pattern"].default,
        description="The dev merge tag grammar; the group named stamp orders tags",
    )


class ModelSiblingPinHookRevsApplyResult(BaseModel):
    """The plan, the hook revs it was read from and the config text with the planned blocks moved."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan: ModelSiblingPinHookRevsPlan
    hook_revs: tuple[str, ...] = Field(
        description="The rev of every hook block of the repository found in the text, in file order"
    )
    candidate_text: str = Field(
        description="The config text with the planned blocks moved to the plan's hook tag; the input text when none moves"
    )
