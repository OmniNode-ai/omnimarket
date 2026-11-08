# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployed adapters of the watcher refresh: the state clock and the subprocess step runner (OMN-23033)."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_repo_state_query_effect.handlers.adapter_repo_state_mirrors import (
    PrWatcherStateFile,
)
from omnimarket.nodes.node_repo_state_watcher_refresh_effect.handlers.handler_watcher_refresh import (
    HandlerWatcherRefresh,
)
from omnimarket.nodes.node_repo_state_watcher_refresh_effect.models.model_watcher_refresh import (
    OVERLAY_FILE_ENV,
    OVERLAY_ID,
    ModelWatcherRefreshOverlay,
    ModelWatcherRefreshStep,
)


class HandlerSubprocessStep:
    """Run one declared step as a subprocess with the overlay's timeout."""

    def __init__(self, timeout_seconds: float) -> None:
        self._timeout = timeout_seconds

    def run(self, name: str, argv: Sequence[str]) -> ModelWatcherRefreshStep:
        started = time.monotonic()
        try:
            done = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ModelWatcherRefreshStep(
                name=name,
                ok=False,
                returncode=None,
                duration_ms=int((time.monotonic() - started) * 1000),
                tail=f"{type(exc).__name__}: {exc}"[-500:],
            )
        return ModelWatcherRefreshStep(
            name=name,
            ok=done.returncode == 0,
            returncode=done.returncode,
            duration_ms=int((time.monotonic() - started) * 1000),
            tail=(done.stderr or done.stdout or "").strip()[-500:],
        )


def load_overlay(path: Path | None = None) -> ModelWatcherRefreshOverlay:
    explicit = yaml.safe_load(path.read_text(encoding="utf-8")) if path else None
    return load_structured_overlay(
        OVERLAY_ID,
        ModelWatcherRefreshOverlay,
        explicit=explicit,
        file_env=OVERLAY_FILE_ENV,
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise NodeOverlayError(
            f"the overlay names {name}, and it is unset on this host"
        )
    return value


def build_handler(overlay: ModelWatcherRefreshOverlay) -> HandlerWatcherRefresh:
    """The deployed handler: the host's tick step, its state-sync step when declared."""
    steps: list[tuple[str, list[str]]] = [
        ("tick", shlex.split(_required_env(overlay.tick_command_env)))
    ]
    if overlay.state_sync_command_env:
        sync = os.environ.get(overlay.state_sync_command_env, "").strip()
        if sync:
            steps.append(("state-sync", shlex.split(sync)))
    return HandlerWatcherRefresh(
        clock=PrWatcherStateFile(Path(_required_env(overlay.watcher_state_env))),
        steps=steps,
        runner=HandlerSubprocessStep(overlay.step_timeout_seconds),
    )


__all__ = ["HandlerSubprocessStep", "build_handler", "load_overlay"]
