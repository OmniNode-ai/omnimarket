# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of node_onex_installer_render_compute (OMN-25070).

The request carries every value the customer installer pins: the customer
package and its version, the Python it installs under, the launchers it must
leave on PATH, and the uv release it bootstraps with the sha256 of each
platform archive. Each field is checked here, so a malformed release input is
refused before any script text exists.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

UV_TARGETS: tuple[str, ...] = (
    "aarch64-apple-darwin",
    "x86_64-apple-darwin",
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
)

_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_VERSION = re.compile(r"^[0-9]+(\.[0-9]+)*((\.post|\.dev|a|b|rc)[0-9]+)*$")
_NUMERIC_VERSION = re.compile(r"^[0-9]+(\.[0-9]+)+$")
_PYTHON = re.compile(r"^3\.[0-9]+$")
_LAUNCHER = re.compile(r"^[a-z][a-z0-9-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ModelOnexInstallerRequest(BaseModel):
    """The pinned values the installer is rendered from."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    package: str = Field(description="Customer package name on PyPI.")
    version: str = Field(description="Exact version the installer installs.")
    python: str = Field(description="Python minor version, for example 3.12.")
    launchers: tuple[str, ...] = Field(
        description="Commands the install must leave on PATH."
    )
    uv_version: str = Field(description="Pinned uv release the installer downloads.")
    uv_min_version: str = Field(
        description="Oldest uv already on PATH the installer will use instead."
    )
    uv_sha256: dict[str, str] = Field(
        description="sha256 of uv-<target>.tar.gz for each supported target."
    )

    @field_validator("package")
    @classmethod
    def _package(cls, value: str) -> str:
        if not _PACKAGE.match(value):
            raise ValueError(f"package {value!r} is not a valid distribution name")
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if not _VERSION.match(value):
            raise ValueError(f"version {value!r} is not a release version")
        return value

    @field_validator("python")
    @classmethod
    def _python(cls, value: str) -> str:
        if not _PYTHON.match(value):
            raise ValueError(f"python {value!r} is not a 3.x minor version")
        return value

    @field_validator("launchers")
    @classmethod
    def _launchers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not _LAUNCHER.match(name) for name in value):
            raise ValueError(f"launchers {value!r} must be non-empty command names")
        if len(set(value)) != len(value):
            raise ValueError(f"launchers {value!r} repeat a name")
        return value

    @field_validator("uv_version", "uv_min_version")
    @classmethod
    def _uv_version(cls, value: str) -> str:
        if not _NUMERIC_VERSION.match(value):
            raise ValueError(f"uv version {value!r} is not numeric")
        return value

    @field_validator("uv_sha256")
    @classmethod
    def _uv_sha256(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) != set(UV_TARGETS):
            raise ValueError(
                f"uv_sha256 must name exactly {list(UV_TARGETS)}, got {sorted(value)}"
            )
        bad = [target for target, digest in value.items() if not _SHA256.match(digest)]
        if bad:
            raise ValueError(f"uv_sha256 for {bad} is not 64 lowercase hex digits")
        return value


class ModelOnexInstallerResult(BaseModel):
    """The rendered installer and its sha256."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    script: str = Field(description="The installer, a Bash script.")
    sha256: str = Field(description="sha256 of the script's UTF-8 bytes.")
