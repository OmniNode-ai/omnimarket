# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Measured Mac facts; absent readings are never represented as measured zero."""

from pydantic import BaseModel, ConfigDict, Field


class ModelSetupMachineFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: str
    architecture: str
    macos_version: str
    cpus: int | None = Field(default=None, ge=1)
    ram_bytes: int | None = Field(default=None, ge=1)
    free_disk_bytes: int = Field(ge=0)
    administrator: bool | None = None
    virtual_machine: bool | None = None
    rosetta: bool | None = None
    docker_installed: bool = False
    stack_running: bool = False
    occupied_ports: tuple[int, ...] = ()
    missing_tools: tuple[str, ...] = ()
