# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_lab_check_run_effect: every setting of the lab check-run poster.

Found at ``<root>/node_lab_check_run_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships two examples with synthetic values: an in-memory backend (the default way to run it) and a GitHub
backend. Nothing here has a default: an absent key refuses the load and names it. The token is never stored:
``token_env`` names the environment variable that holds it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_lab_check_run_effect.models.model_lab_check_run import (
    ModelCheckRunRecord,
)

NODE_NAME = "node_lab_check_run_effect"


class ModelLabCheckRunSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The required check's name, the one branch protection names.
    check_name: str = Field(min_length=1)
    #: How long after the dispatch the check may take to post; the landing decision's ``lab_check_wait_seconds``.
    wait_bound_seconds: int = Field(ge=1)
    #: The repositories whose jobs run on a lab runner. A post for any other repository is refused before a call.
    lab_repositories: tuple[str, ...] = Field(min_length=1)
    #: What a superseded attempt's run is set to. Neither value counts as a pass for a required check.
    superseded_conclusion: Literal["cancelled", "failure"]
    #: Conclusions GitHub counts as a pass for a required check; a run of another attempt holding one is invalidated.
    passing_conclusions: tuple[str, ...] = Field(min_length=1)
    title_pending: str = Field(min_length=1)
    title_success: str = Field(min_length=1)
    title_failure: str = Field(min_length=1)
    title_superseded: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelLabCheckRunSettings:
        for repo in self.lab_repositories:
            parts = repo.split("/")
            if len(parts) != 2 or not all(parts):
                raise ValueError("a lab repository is named owner/name")
        if self.superseded_conclusion in self.passing_conclusions:
            raise ValueError("the superseded conclusion must not be a passing one")
        return self


class ModelInMemorySource(BaseModel):
    """The in-memory API, seeded with the runs already on the head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["inmemory"]
    fixture: tuple[ModelCheckRunRecord, ...] = ()


class ModelGithubSource(BaseModel):
    """The GitHub check-run REST API."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["github"]
    api_base: str = Field(min_length=1)
    api_version: str = Field(min_length=1)
    #: The NAME of the environment variable holding the App installation token; the value is never stored.
    token_env: str = Field(min_length=1)
    #: Calls one handle may make; the call past it is not made.
    max_calls: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    timeout_seconds: float = Field(gt=0)


ModelCheckRunSource = Annotated[
    ModelInMemorySource | ModelGithubSource, Field(discriminator="backend")
]


class ModelEmissionBase(BaseModel):
    """The floor emission identity of this node's chain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str = Field(min_length=1)
    #: The piece this node is on the floor (rebuild plan 3.6: piece 8).
    piece: int = Field(ge=1)


class ModelInMemoryEmission(ModelEmissionBase):
    """Events stay in the handler's sink."""

    backend: Literal["inmemory"]


class ModelJournalEmission(ModelEmissionBase):
    """Events are appended to a JSON-lines file."""

    backend: Literal["journal"]
    journal_path: str = Field(min_length=1)

    def journal(self) -> Path:
        return Path(self.journal_path)


#: The backend key picks the model; no code compares it with a literal.
ModelEmissionSettings = Annotated[
    ModelInMemoryEmission | ModelJournalEmission, Field(discriminator="backend")
]


class ModelLabCheckRunOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    settings: ModelLabCheckRunSettings
    source: ModelCheckRunSource
    emission: ModelEmissionSettings


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelLabCheckRunOverlay:
    return load_structured_overlay(NODE_NAME, ModelLabCheckRunOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelCheckRunSource",
    "ModelEmissionBase",
    "ModelEmissionSettings",
    "ModelGithubSource",
    "ModelInMemoryEmission",
    "ModelInMemorySource",
    "ModelJournalEmission",
    "ModelLabCheckRunOverlay",
    "ModelLabCheckRunSettings",
    "load_overlay",
]
