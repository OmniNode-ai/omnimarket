# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_review_gate_context_effect: every setting of the review context poster.

Found at ``<root>/node_review_gate_context_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``.
The package ships two examples with synthetic values: an in-memory backend (the default way to run it) and a bus
backend (the real one: the write node and the gateway node over the lab bus). Nothing here has a default: an
absent key refuses the load and names it. No token is stored anywhere in the overlay.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_github_gateway_io import ModelGithubGatewayRequest
from omnimarket.models.model_repo_write_io import ModelRepoWriteRequest
from omnimarket.nodes.node_lab_check_run_effect.overlay import ModelEmissionSettings
from omnimarket.nodes.node_review_gate_context_effect.models.model_review_context_post import (
    ModelHeadCheckRun,
)
from omnimarket.nodes.node_review_gate_context_effect.protocols.protocol_head_check_runs import (
    ProtocolHeadCheckRunPoster,
    ProtocolHeadCheckRunReader,
)

NODE_NAME = "node_review_gate_context_effect"


class ModelReviewContextBusRoute(BaseModel):
    """The bus route: the lane, the NAME of the environment variable holding the workspace root and the timeout."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["bus"]
    bus_lane: str = Field(min_length=1)
    workspace_env: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    requested_by_lane: str = Field(min_length=1)


class ModelReviewContextInMemoryRoute(BaseModel):
    """The in-memory route: the runs already on the head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["inmemory"]
    fixture: tuple[ModelHeadCheckRun, ...] = ()


#: The backend key picks the model; no code compares it with a literal.
ModelReviewContextRoute = Annotated[
    ModelReviewContextBusRoute | ModelReviewContextInMemoryRoute,
    Field(discriminator="backend"),
]


class ModelReviewContextOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    route: ModelReviewContextRoute
    emission: ModelEmissionSettings


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelReviewContextOverlay:
    return load_structured_overlay(NODE_NAME, ModelReviewContextOverlay, environ)


def build_ports(
    overlay: ModelReviewContextOverlay, environ: Mapping[str, str]
) -> tuple[ProtocolHeadCheckRunReader, ProtocolHeadCheckRunPoster]:
    """Returns the reader and poster for the overlay's route."""
    from omnimarket.nodes.node_review_gate_context_effect.adapters.inmemory_head_check_runs import (
        InMemoryHeadCheckRuns,
    )

    if isinstance(overlay.route, ModelReviewContextInMemoryRoute):
        adapter = InMemoryHeadCheckRuns(overlay.route.fixture)
        return adapter, adapter

    from omnimarket.models.scm import EnumScmNode
    from omnimarket.nodes.node_review_gate_context_effect.adapters.bus_scm_call import (
        bus_scm_call,
    )
    from omnimarket.nodes.node_review_gate_context_effect.adapters.gateway_check_run_reader import (
        GatewayCheckRunReader,
    )
    from omnimarket.nodes.node_review_gate_context_effect.adapters.write_node_check_run_poster import (
        WriteNodeCheckRunPoster,
    )

    route = overlay.route

    def poster_call(
        request: ModelRepoWriteRequest,
    ) -> tuple[bool, dict[str, JsonValue]]:
        return bus_scm_call(
            route=route,
            environ=environ,
            node=EnumScmNode.WRITE,
            request=request,
            correlation_id=request.correlation_id,
        )

    def reader_call(
        request: ModelGithubGatewayRequest,
    ) -> tuple[bool, dict[str, JsonValue]]:
        return bus_scm_call(
            route=route,
            environ=environ,
            node=EnumScmNode.GATEWAY,
            request=request,
            correlation_id=request.correlation_id,
        )

    poster = WriteNodeCheckRunPoster(route, poster_call)
    reader = GatewayCheckRunReader(route, reader_call)
    return reader, poster


__all__ = [
    "NODE_NAME",
    "ModelReviewContextBusRoute",
    "ModelReviewContextInMemoryRoute",
    "ModelReviewContextOverlay",
    "ModelReviewContextRoute",
    "build_ports",
    "load_overlay",
]
