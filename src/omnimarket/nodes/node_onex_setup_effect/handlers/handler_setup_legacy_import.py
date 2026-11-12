# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read old choices as bounded, inert data without changing the source files."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from omnimarket.nodes.node_onex_setup_effect.models.model_setup_legacy_hints import (
    ModelSetupLegacyHints,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_legacy_import_request import (
    ModelSetupLegacyImportRequest,
)

_MAX_BYTES = 65536


def _read(path: Path) -> str | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
        raise ValueError("Invalid legacy onboarding state")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        data = source.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise ValueError("Invalid legacy onboarding state")
    return data.decode("utf-8")


class HandlerSetupLegacyImport:
    """Import only previous selections; phase marks are never ready states."""

    def handle(
        self, request: ModelSetupLegacyImportRequest | Path
    ) -> ModelSetupLegacyHints | None:
        """Read known files once under the calling workflow's setup lock."""
        directory = (
            request.directory
            if isinstance(request, ModelSetupLegacyImportRequest)
            else request
        )
        if directory.is_symlink():
            raise ValueError("Invalid legacy onboarding state")
        if not directory.exists():
            return None
        if not directory.is_dir():
            raise ValueError("Invalid legacy onboarding state")
        try:
            files = {
                name: _read(directory / name)
                for name in ("choices", "phases.v2.done", "stack.anchor")
            }
            if all(value is None for value in files.values()):
                return None
            choices: dict[str, str] = {}
            for line in (files["choices"] or "").splitlines():
                if not line.strip():
                    continue
                key, separator, value = line.partition("=")
                if (
                    not separator
                    or key not in {"models", "containers", "local_url", "local_model"}
                    or key in choices
                ):
                    raise ValueError
                choices[key] = value
            if "models" in choices and not choices["models"].split():
                raise ValueError
            if "containers" in choices and choices["containers"] not in ("0", "1"):
                raise ValueError
            payload: dict[str, object] = {
                "providers": tuple(choices.get("models", "").split()),
                "containers": choices["containers"] == "1"
                if "containers" in choices
                else None,
                "local_url": choices.get("local_url"),
                "local_model": choices.get("local_model"),
                "completed_phases": tuple(
                    int(line)
                    for line in (files["phases.v2.done"] or "").splitlines()
                    if line.strip()
                ),
                "infra_anchor": (files["stack.anchor"] or "").strip() or None,
            }
            return ModelSetupLegacyHints.model_validate(payload)
        except (ValueError, UnicodeError):
            # Parser and Pydantic errors may include input; expose neither.
            raise ValueError("Invalid legacy onboarding state") from None
