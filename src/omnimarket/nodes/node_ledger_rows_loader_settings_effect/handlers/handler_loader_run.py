# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the ledger rows loader the way this host's overlays say, and say whether the reader may read the file (OMN-24059).

The plugin's shared ledger readers call this node through the dispatch venv with the loader's arguments. The node resolves the
host's loader command and bound (HandlerLoaderSettings), runs the loader with that bound and maps what happened to one of four
outcomes: ``answered`` (the loader's rows), ``file-fallback-answered`` (the loader read the file itself, counted by the
loader), ``fallback-allowed`` (no loader answer, the reason stated, and the loader overlay allows the reader to read the file)
and ``unreadable`` (no answer and the overlay forbids the file, or an overlay is missing or malformed, or the loader reports
the ledger unreadable): the reader raises with the reason, never an empty list.
Named break: a handler that returns fallback-allowed while the loader overlay forbids the file fallback.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

from omnimarket.nodes.node_ledger_rows_loader_settings_effect.handlers.handler_loader_settings import (
    HandlerLoaderSettings,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.models import (
    EnumLoaderRunStatus,
    EnumLoaderSettingsStatus,
    ModelLoaderRunRequest,
    ModelLoaderRunResult,
    ModelLoaderSettingsRequest,
    ModelLoaderSettingsResult,
)

LOADER_EXIT_FILE_FALLBACK = 10
LOADER_EXIT_UNREADABLE = 69
FORBIDS_FALLBACK = (
    "the node_ledger_rows_loader_effect overlay forbids the file fallback"
)


class HandlerLoaderRun:
    """``handle(ModelLoaderRunRequest) -> ModelLoaderRunResult``."""

    def __init__(
        self,
        settings: HandlerLoaderSettings,
        run: Callable[..., Any] = subprocess.run,
    ) -> None:
        self._settings = settings
        self._run = run

    def handle(self, request: ModelLoaderRunRequest) -> ModelLoaderRunResult:
        settings = self._settings.handle(
            ModelLoaderSettingsRequest(
                registry_root=request.registry_root, host=request.host
            )
        )
        if settings.status is EnumLoaderSettingsStatus.REFUSED:
            return self._finish(
                settings, EnumLoaderRunStatus.UNREADABLE, settings.reason
            )
        if settings.argv is None:
            return self._miss(settings, settings.reason)
        try:
            proc = self._run(
                [*settings.argv, *request.loader_args],
                capture_output=True,
                text=True,
                timeout=settings.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return self._miss(settings, "loader-timeout")
        except OSError:
            return self._miss(settings, "loader-not-runnable")
        if proc.returncode == 0:
            return self._finish(
                settings, EnumLoaderRunStatus.ANSWERED, "loader-answered", proc
            )
        if proc.returncode == LOADER_EXIT_FILE_FALLBACK:
            return self._finish(
                settings,
                EnumLoaderRunStatus.FILE_FALLBACK_ANSWERED,
                "loader-file-fallback",
                proc,
            )
        if proc.returncode == LOADER_EXIT_UNREADABLE:
            return self._finish(
                settings,
                EnumLoaderRunStatus.UNREADABLE,
                f"loader-unreadable: {' '.join(proc.stderr.split())[-300:]}",
                proc,
            )
        return self._miss(settings, f"loader-failed-exit-{proc.returncode}", proc)

    def _miss(
        self,
        settings: ModelLoaderSettingsResult,
        reason: str,
        proc: Any = None,
    ) -> ModelLoaderRunResult:
        """No loader answer: the reader may read the file only when the loader overlay allows it."""
        if settings.file_fallback_allowed:
            return self._finish(
                settings, EnumLoaderRunStatus.FALLBACK_ALLOWED, reason, proc
            )
        return self._finish(
            settings,
            EnumLoaderRunStatus.UNREADABLE,
            f"{reason}; {FORBIDS_FALLBACK}",
            proc,
        )

    @staticmethod
    def _finish(
        settings: ModelLoaderSettingsResult,
        status: EnumLoaderRunStatus,
        reason: str,
        proc: Any = None,
    ) -> ModelLoaderRunResult:
        answered = status in (
            EnumLoaderRunStatus.ANSWERED,
            EnumLoaderRunStatus.FILE_FALLBACK_ANSWERED,
        )
        return ModelLoaderRunResult(
            status=status,
            reason=reason,
            stdout=proc.stdout if answered and proc is not None else "",
            stderr=proc.stderr if proc is not None else "",
            settings=settings,
        )


__all__ = ["HandlerLoaderRun"]
