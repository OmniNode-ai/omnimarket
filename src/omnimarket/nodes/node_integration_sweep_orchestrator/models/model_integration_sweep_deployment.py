# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment facts of the integration sweep: where its probes reach.

Supplied by whoever runs the sweep through the node's overlay; the package ships
none of them. A request value, when set, wins over the overlay.
"""

from pydantic import BaseModel, ConfigDict, Field


class ModelIntegrationSweepDeployment(BaseModel):
    """Runtime hosts, endpoints and repo path the probes target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_host: str = Field(
        default="", description="Runtime SSH host for runtime_sha_match probes."
    )
    runtime_repo_path: str = Field(
        default="",
        description="Repo path on the runtime host used by the SSH git SHA probe.",
    )
    stability_test_runtime_url: str = Field(
        default="",
        description="URL of the runtime health endpoint probed by RUNTIME_HEALTH.",
    )
    container_health_host: str = Field(
        default="", description="SSH host for the CONTAINER_HEALTH probe."
    )
    infra_runtime_host: str = Field(
        default="",
        description="SSH host for the KAFKA / DB / GOLDEN_CHAIN probes.",
    )
    projection_api_url: str = Field(
        default="", description="Base URL of the projection API of the runtime lane."
    )
