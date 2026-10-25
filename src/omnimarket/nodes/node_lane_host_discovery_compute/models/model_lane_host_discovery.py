# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and rendered shell lines for a remote lane's host-local inputs."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

# omnimarket's onex-prrp-snapshot-publish (OMN-19998) copies the runner-placement snapshot here, under $HOME,
# on every lab host.
PRRP_SNAPSHOT_DESTINATION = ".omninode/prrp/variables-snapshot.json"


class ModelLaneHostDiscoveryRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    knowledge_base: str | None = None
    knowledge_base_home_env: str | None = None
    registry_root_var: str = "ONEX_REGISTRY_ROOT"
    environ: Mapping[str, str] | None = None
    prrp_snapshot_destination: str = PRRP_SNAPSHOT_DESTINATION


class ModelLaneHostDiscoveryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kb_lines: tuple[str, ...]
    prrp_snapshot_lines: tuple[str, ...]
