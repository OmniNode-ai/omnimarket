# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input command for node_model_eval_orchestrator (OMN-13615).

Absorbs the ``ModelEndpointConfig`` shape from the SEA ``eval/models.py`` and
the run inputs from ``EvalRunner.run``. Strongly typed and frozen: the endpoint
list is a tuple of frozen models and ``correlation_id`` is a ``UUID``.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelEndpointConfig(BaseModel):
    """A single LLM endpoint to evaluate.

    ``api_key`` is an optional literal secret carried only for the in-process
    eval path; production callers resolve it from the secret store at the effect
    boundary. ``provider`` distinguishes local (free) from cloud (cost-bearing)
    backends so the cost-efficiency score can be computed without network I/O.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str = Field(
        min_length=1, description="Model identifier sent to the endpoint."
    )
    endpoint: str = Field(
        min_length=1,
        description="Full OpenAI-compatible chat-completions URL for the model.",
    )
    api_key: str | None = Field(
        default=None,
        description="Optional bearer token for the endpoint (cloud providers).",
    )
    provider: str = Field(
        default="",
        description="Provider class, e.g. 'local_vllm' or 'cloud_gemini'.",
    )


class ModelModelEvalStart(BaseModel):
    """Command envelope payload that starts a model-evaluation experiment run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = Field(
        min_length=1,
        description="Generation prompt sent identically to every endpoint.",
    )
    endpoints: tuple[ModelEndpointConfig, ...] = Field(
        ...,
        min_length=1,
        description="Endpoints to evaluate; at least one is required.",
    )
    correlation_id: UUID = Field(
        ...,
        description="Correlation identifier linking events across the run.",
    )


__all__ = ["ModelEndpointConfig", "ModelModelEvalStart"]
