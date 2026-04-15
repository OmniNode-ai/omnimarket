# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plugin compatibility version handshake for the ONEX runtime.

Reads plugin-compat.yaml from the omniclaude plugin bundle (if present),
compares declared min/max_runtime_version against the installed omnimarket
package version, and returns a structured result.

On mismatch: logs a warning naming the incompatible versions. Does NOT raise —
fail-soft so the runtime can still start with degraded plugin support.

Called from: omnimarket startup / `onex health` command (OMN-8789).
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import yaml
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_DEFAULT_COMPAT_PATHS: list[Path] = [
    Path(__file__).parent.parent.parent.parent.parent
    / "plugins"
    / "onex"
    / "plugin-compat.yaml",
]


class ModelNodeTopicDeclaration(BaseModel):
    """Single topic entry declared in plugin-compat.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    schema_version: str
    role: str


class ModelNodeCompatEntry(BaseModel):
    """Per-node compatibility entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str
    topics: list[ModelNodeTopicDeclaration] = Field(default_factory=list)


class ModelPluginCompatMatrix(BaseModel):
    """Parsed plugin-compat.yaml document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plugin: str
    plugin_version: str
    min_runtime_version: str
    max_runtime_version: str
    nodes: list[ModelNodeCompatEntry] = Field(default_factory=list)


class ModelPluginCompatResult(BaseModel):
    """Result returned by check_plugin_compat()."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    compatible: bool
    plugin_version: str | None = None
    runtime_version: str | None = None
    min_runtime_version: str | None = None
    max_runtime_version: str | None = None
    compat_path: str | None = None
    skip_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)


def _installed_version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def load_compat_matrix(
    compat_path: Path | None = None,
) -> ModelPluginCompatMatrix | None:
    """Load and parse plugin-compat.yaml. Returns None if not found."""
    candidates: list[Path] = [compat_path] if compat_path else _DEFAULT_COMPAT_PATHS
    for path in candidates:
        if path.is_file():
            raw: Any = yaml.safe_load(path.read_text())
            return ModelPluginCompatMatrix.model_validate(raw)
    return None


def check_plugin_compat(
    compat_path: Path | None = None,
    runtime_package: str = "omnimarket",
) -> ModelPluginCompatResult:
    """Check plugin-compat.yaml against the installed runtime version.

    Returns ModelPluginCompatResult with compatible=True on success or if the
    compat file is absent (graceful skip). On mismatch, compatible=False and
    warnings are populated — caller decides whether to hard-fail.
    """
    matrix = load_compat_matrix(compat_path)
    if matrix is None:
        return ModelPluginCompatResult(
            compatible=True,
            skip_reason="plugin-compat.yaml not found; skipping version check",
        )

    runtime_ver_str = _installed_version(runtime_package)
    if runtime_ver_str is None:
        return ModelPluginCompatResult(
            compatible=True,
            plugin_version=matrix.plugin_version,
            skip_reason=f"package '{runtime_package}' not installed; skipping version check",
        )

    runtime_ver = Version(runtime_ver_str)
    min_ver = Version(matrix.min_runtime_version)
    max_ver = Version(matrix.max_runtime_version)

    warnings: list[str] = []
    compatible = True

    if runtime_ver < min_ver:
        compatible = False
        msg = (
            f"Plugin '{matrix.plugin}' v{matrix.plugin_version} requires runtime "
            f">={matrix.min_runtime_version}, but installed {runtime_package}=={runtime_ver_str}. "
            f"Upgrade {runtime_package} to >={matrix.min_runtime_version}."
        )
        warnings.append(msg)
        logger.warning(msg)

    if runtime_ver >= max_ver:
        compatible = False
        msg = (
            f"Plugin '{matrix.plugin}' v{matrix.plugin_version} requires runtime "
            f"<{matrix.max_runtime_version}, but installed {runtime_package}=={runtime_ver_str}. "
            f"Plugin may be incompatible with this runtime version — check plugin-compat.yaml."
        )
        warnings.append(msg)
        logger.warning(msg)

    if compatible:
        logger.debug(
            "Plugin '%s' v%s compatible with %s==%s (window: [%s, %s))",
            matrix.plugin,
            matrix.plugin_version,
            runtime_package,
            runtime_ver_str,
            matrix.min_runtime_version,
            matrix.max_runtime_version,
        )

    return ModelPluginCompatResult(
        compatible=compatible,
        plugin_version=matrix.plugin_version,
        runtime_version=runtime_ver_str,
        min_runtime_version=matrix.min_runtime_version,
        max_runtime_version=matrix.max_runtime_version,
        warnings=warnings,
    )
