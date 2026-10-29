# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Backlog admission shared by the idle-slot fill and SM-00 (OMN-21475).

Both nodes validate the same crossing without importing a sibling node's private models.
"""

from __future__ import annotations

from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from omnimarket.models.model_engine_ladder import ladder_of

POLICY_CONTEXT_KEY = "lane_model_policy"
REPO_KINDS_CONTEXT_KEY = "repo_required_kinds"


def _normal(value: str) -> str:
    return value.strip().lower().replace("_", "-")


class ModelBacklogItem(BaseModel):
    """One backlog item as the fill and SM-00 read it (OMN-21475, T-S3).

    Only the crossing's shape is checked: a key, a kind, a repository and a ``required_engine``
    that is empty (no requirement) or an engine the system knows. That is a rung or host engine of
    the ladder (``config.engine_ladder`` of ``node_remote_lane_compute``) or a family, alias or
    model of the adopted lane model policy (``claude``, ``claude-sonnet``, ``sonnet``). Free text
    is not an engine, so an item carrying it is never dispatched on a guessed one. The fields the
    fill reads beyond these pass through.

    Validation context: ``engine_ladder`` (a ``ModelEngineLadder``), ``lane_model_policy`` (a
    ``ModelLaneModelPolicy``) and ``repo_required_kinds`` (kinds that must name a repository, from
    the ``idle_slot_fill`` overlay); each defaults to the packaged declaration, and no kind needs a
    repository unless the overlay lists it.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    key: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    repo: str = ""
    required_engine: str | None = None

    @field_validator("repo", mode="before")
    @classmethod
    def absent_repo_is_empty(cls, value: object) -> object:
        return "" if value is None else value

    @field_validator("required_engine", mode="before")
    @classmethod
    def known_engine(cls, value: object, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("required_engine must be text")
        text = value.strip()
        if not text:
            return None
        from omnimarket.nodes.node_lane_model_policy_compute.handlers.handler_lane_model_policy import (
            load_policy,
        )

        context = info.context if isinstance(info.context, dict) else {}
        policy = context.get(POLICY_CONTEXT_KEY) or load_policy()
        ladder = ladder_of(info)
        known = {
            _normal(name)
            for name in (
                *ladder.rungs,
                *ladder.host_engines,
                *policy.order,
                *policy.engines.values(),
                *policy.aliases,
            )
        }
        if _normal(text) not in known:
            raise ValueError(
                f"required_engine {text!r} is not an engine of the ladder or the lane model policy"
            )
        return text

    @field_validator("repo")
    @classmethod
    def repo_where_the_kind_needs_one(cls, value: str, info: ValidationInfo) -> str:
        context = info.context if isinstance(info.context, dict) else {}
        kinds = context.get(REPO_KINDS_CONTEXT_KEY, ())
        if not value.strip() and info.data.get("kind") in kinds:
            raise ValueError(f"repo is required for kind {info.data['kind']!r}")
        return value


def backlog_item_refusal(
    raw: object, *, context: dict[str, Any] | None = None
) -> str | None:
    """Why ``raw`` is not a backlog item, or None when it is one."""
    try:
        ModelBacklogItem.model_validate(raw, context=context)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        return f"{where}: {first['msg'].removeprefix('Value error, ')}"
    return None
