# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the host observability installer.

The settings document itself is the shared overlay model in ``omnimarket.models.overlay_documents``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.overlay_documents.model_observability_overlay import (
    Mode,
    ModelObservabilityOverlay,
    NonEmptyStr,
)

__all__ = [
    "ModelObservabilityInstallRequest",
    "ModelObservabilityInstallResult",
    "ModelObservabilityOverlay",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelObservabilityInstallRequest(_Frozen):
    """``host_name`` is the lab_hosts row this run installs on; ``overlay_file`` and ``hosts_file`` pin the sources."""

    host_name: NonEmptyStr
    mode: Mode
    overlay_file: str = ""
    hosts_file: str = ""


class ModelObservabilityInstallResult(_Frozen):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    uncovered_hosts: tuple[str, ...] = ()
