# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the access-mode detector (OMN-20892)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelAccessModeRequest:
    """Everything the mode decision reads, gathered once by the caller.

    ``environ`` is the hook process's environment, which Claude Code hands down from the session
    process. ``transcript_entrypoint`` is the ``entrypoint`` field of the session transcript's
    first rows, read only when the environment carries no entrypoint. ``state_dir`` holds the
    per-session state files and the global override.
    """

    session_id: str
    state_dir: Path
    environ: dict[str, str] = field(default_factory=dict)
    transcript_entrypoint: str = ""
