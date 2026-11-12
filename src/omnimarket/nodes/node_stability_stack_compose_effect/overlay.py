# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_stability_stack_compose_effect: the host and command budgets, none in code.

Found at ``<root>/node_stability_stack_compose_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The package
ships ``overlay.example.yaml`` with synthetic values. ``local_host`` is the lab host this effect runs on; a stack whose host
differs is refused before any command. Every timeout and the docker binary come from here, so no host, binary or timeout is
written in code. Nothing here has a default: an absent key refuses the load.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_stability_stack_compose_effect"


class ModelStackComposeOverlay(BaseModel):
    """The effect's own facts: where it runs, what it invokes, how long it waits."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The lab host name this effect runs on; compared with the stack's host.
    local_host: str = Field(min_length=1)
    #: The container CLI invoked for every compose, ps and exec command.
    docker_binary: str = Field(min_length=1)
    #: The name of the environment variable naming the directory rendered
    #: compose files are written to; never a path itself.
    render_dir_env: str = Field(min_length=1)
    command_timeout_seconds: int = Field(ge=1)
    up_wait_seconds: int = Field(ge=1)
    health_timeout_seconds: int = Field(ge=1)
    event_timeout_seconds: int = Field(ge=1)


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelStackComposeOverlay:
    """Load the node's overlay from the first root in ``ONEX_SKILL_OVERLAY_ROOTS`` holding one."""
    return load_structured_overlay(NODE_NAME, ModelStackComposeOverlay, environ)


__all__ = ["NODE_NAME", "ModelStackComposeOverlay", "load_overlay"]
