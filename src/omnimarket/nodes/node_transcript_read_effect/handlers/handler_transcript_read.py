# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerTranscriptRead: read one pinned transcript under ONEX_CONTEXT_REPLAY_ROOT (OMN-21129, TC.6).

Pinned mode only. The root comes from ``ONEX_CONTEXT_REPLAY_ROOT`` and has no default:
unset or empty is a refusal, never a fall-back to a home or project directory. The
request names a path relative to the root; an absolute path, or one that resolves
outside the root through ``..`` or a symlink, is refused before any file is opened.
Production reads ``session_content``; this node exists for replay cases only.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path, PurePath

from omnimarket.models.transcript_parse import (
    EnumTranscriptReadStatus,
    ModelTranscriptReadRequest,
    ModelTranscriptReadResult,
)

REPLAY_ROOT_ENV = "ONEX_CONTEXT_REPLAY_ROOT"


class HandlerTranscriptRead:
    """Read a transcript file's bytes under the replay root and return its UTF-8 text."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def handle(self, request: ModelTranscriptReadRequest) -> ModelTranscriptReadResult:
        relative = request.relative_path

        def refused(status: EnumTranscriptReadStatus) -> ModelTranscriptReadResult:
            return ModelTranscriptReadResult(status=status, relative_path=relative)

        root_value = self._environ.get(REPLAY_ROOT_ENV, "")
        if not root_value.strip():
            return refused(EnumTranscriptReadStatus.REFUSED_ROOT_UNSET)
        if PurePath(relative).is_absolute():
            return refused(EnumTranscriptReadStatus.REFUSED_OUTSIDE_ROOT)
        root = Path(root_value).resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or target == root:
            return refused(EnumTranscriptReadStatus.REFUSED_OUTSIDE_ROOT)
        if not target.is_file():
            return refused(EnumTranscriptReadStatus.NOT_FOUND)
        data = target.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return refused(EnumTranscriptReadStatus.DECODE_FAILED)
        return ModelTranscriptReadResult(
            status=EnumTranscriptReadStatus.READ,
            relative_path=relative,
            transcript_text=text,
            byte_count=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )


__all__ = ["REPLAY_ROOT_ENV", "HandlerTranscriptRead"]
