# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the Linux host net unit installer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelNetUnitsRequest(BaseModel):
    """Install, inspect or remove one declared root-owned host unit of a Linux pool host.

    ``overlay_root`` is the directory that holds ``units.yaml`` and the payload files the unit installs. ``host_name``
    is the pool name of the host the request runs on; the unit installs only on a host its declaration names.
    ``units_file`` names the document inside ``overlay_root``; empty means the contract's default ``units.yaml``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: str
    mode: Literal["dry-run", "install", "status", "uninstall"] = "dry-run"
    host_name: str
    overlay_root: str
    units_file: str = ""


class ModelNetUnitsResult(BaseModel):
    """``status`` is ``success``, ``failure`` or ``refused``; a refusal carries a typed ``reason``.

    ``changed`` says whether the request wrote, removed or started anything (a dry-run: would). ``rendered`` is the text of
    a payload that is rendered from the host, ``interface`` the host interface it was rendered with, and ``digests``
    the sha256 of each payload's bytes by destination.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: str
    mode: str
    status: str
    exit_code: int = 0
    reason: str = ""
    stdout: str = ""
    stderr: str = ""
    changed: bool = False
    rendered: str = ""
    interface: str = ""
    digests: dict[str, str] = Field(default_factory=dict)
    gpu_memory_utilization: str = ""


class ModelGpuBudget(BaseModel):
    """Measured GPU memory budget for a serving unit, in GiB and tokens."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_gib: float
    engine_resident_gib: float
    engine_fraction_measured: float
    co_resident_gib: float
    margin_gib: float
    kv_cache_tokens_measured: int
    kv_bytes_per_token: int
    max_model_len: int
