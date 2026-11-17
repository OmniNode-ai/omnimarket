# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result models for node_branch_deploy_command_effect."""

from __future__ import annotations

import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The deploy agent's own shapes for a sibling_tags entry: the sibling repo the departure pins and the dev tag
# it pins it to (the tag shape the sibling pins themselves carry, e.g. in the sibling-pin train floor).
_SIBLING_REPO = re.compile(r"^[a-z][a-z0-9_]*$")
_SIBLING_TAG = re.compile(r"^dev-[0-9]{14}-[0-9a-f]{12}$")


class ModelBranchDeployCommandRequest(BaseModel):
    """The typed request to sign and publish the deploy agent's rebuild command over the bus.

    ``form`` picks the command's shape: the default ``pr`` form is today's command, built around a pull request
    number; the explicit ``tag_departure`` form is a tag departure, which refuses a pull request number and instead
    carries ``sibling_tags``, the sibling repos' dev tags the departure pins.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    mode: Literal["deploy", "rollback"]
    form: Literal["pr", "tag_departure"] = "pr"
    runtime_lane: str = Field(min_length=1)
    repo: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    pr_number: int | None = Field(default=None, ge=1)
    git_ref: str = Field(pattern=r"^[0-9a-f]{40}$")
    image_ref: str = ""
    overlay_ref: str = Field(default="", pattern=r"^([0-9a-f]{40})?$")
    sibling_tags: dict[str, str] = Field(default_factory=dict)
    rollback_actor: str = Field(min_length=1)
    rollback_reason: str = Field(min_length=1)
    requested_by: str = Field(min_length=1)

    @field_validator("sibling_tags")
    @classmethod
    def _well_shaped_sibling_tags(cls, value: dict[str, str]) -> dict[str, str]:
        for repo, tag in value.items():
            if _SIBLING_REPO.fullmatch(repo) is None:
                raise ValueError(f"sibling_tags key {repo!r} is not a repo name")
            if _SIBLING_TAG.fullmatch(tag) is None:
                raise ValueError(
                    f"sibling_tags value {tag!r} for repo {repo!r} is not a dev tag"
                )
        return value

    @model_validator(mode="after")
    def _form_agrees_with_the_rest(self) -> ModelBranchDeployCommandRequest:
        if self.form == "pr":
            if self.pr_number is None:
                raise ValueError("form 'pr' needs a pr_number")
            if self.sibling_tags:
                raise ValueError(
                    "form 'pr' refuses sibling_tags; the tag-departure form carries them"
                )
        else:
            if self.pr_number is not None:
                raise ValueError(
                    "form 'tag_departure' refuses a pr_number; it carries sibling_tags instead"
                )
            if self.mode != "deploy":
                raise ValueError("form 'tag_departure' needs mode 'deploy'")
            if not self.sibling_tags:
                raise ValueError("form 'tag_departure' needs a non-empty sibling_tags")
        return self


class ModelBranchDeployCommandResult(BaseModel):
    """The typed result of publishing the deploy agent's rebuild command over the bus (no trigger)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    mode: Literal["deploy", "rollback"]
    status: Literal["published", "refused", "failed"]
    detail: str = ""
    # Set only on a key mismatch refusal (OMN-24396): the one-way check values of both keys, never a key.
    signer_key_check: str = ""
    agent_key_check: str = ""
    # Set only when the signer's key was compared with the secret store's key (OMN-24611 verify_env_key); the agent
    # was not asked then, so ``agent_key_check`` stays empty.
    store_key_check: str = ""
