# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide how the operator is reaching this session: local, remote or headless (OMN-20892).

Detection reads the session's own environment, which Claude Code hands to every hook process:

* REMOTE when any client marker is present: ``CLAUDE_CODE_REMOTE=true`` (a cloud session),
  ``CLAUDE_CODE_REMOTE_SESSION_ID`` or ``CLAUDE_CODE_BRIDGE_SESSION_ID`` set,
  ``CLAUDE_CODE_ENVIRONMENT_KIND`` set (``bridge`` is a Remote Control session driven from the
  phone or claude.ai, ``byoc`` a self-hosted cloud runner), or an entrypoint that names a remote
  client (``remote_mobile``, ``remote_desktop``, ``web``, ``claude-in-slack`` and the like).
* HEADLESS for a landing worker (``LANDING_WORKER_TAG``, which wins over inherited client
  markers) or an SDK entrypoint (``sdk-cli``,
  ``sdk-py``, ``sdk-ts``) with no client marker: a ``claude -p`` run nobody reads.
* LOCAL for the interactive CLI (``entrypoint=cli``) with no client marker.
* Anything else is unknown, resolved to REMOTE, the safer side.

The entrypoint comes from ``CLAUDE_CODE_ENTRYPOINT``, else from the transcript's ``entrypoint``.

Overrides, strongest first: ``OMNI_ACCESS_MODE`` in the environment (``local``, ``remote`` or
``headless``); the session's prompt-command override (``mode local`` / ``mode remote``); the global
override file (``onex-access-mode set local``). The two file overrides never turn a headless
session into an operator-facing one.

Known limit: a ``/remote-control`` switched on inside a local CLI session changes no environment
variable, so it reads as LOCAL; say ``mode remote`` in that session. A ``claude -p`` started from
inside a Remote Control session inherits its markers and reads as REMOTE.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..models.enum_access_mode import EnumAccessMode
from ..models.model_access_mode_request import ModelAccessModeRequest
from ..models.model_access_mode_result import ModelAccessModeResult
from .handler_access_mode_state import (
    OVERRIDE_ENV,
    read_global_override,
    read_session,
)

REMOTE_ENTRYPOINT_PREFIXES = ("remote", "mobile", "web", "claude-in-", "claude_in_")
SDK_ENTRYPOINT_PREFIXES = ("sdk",)
LOCAL_ENTRYPOINTS = frozenset({"cli"})


def detect(
    environ: Mapping[str, str], transcript_entrypoint: str = ""
) -> tuple[EnumAccessMode | None, tuple[str, ...]]:
    """Detection alone: the mode the client signals say, or None when they say nothing."""
    if environ.get("LANDING_WORKER_TAG", "").strip():
        return EnumAccessMode.HEADLESS, ("LANDING_WORKER_TAG set",)
    remote: list[str] = []
    if environ.get("CLAUDE_CODE_REMOTE", "").strip().lower() == "true":
        remote.append("CLAUDE_CODE_REMOTE=true")
    for key in ("CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_BRIDGE_SESSION_ID"):
        if environ.get(key, "").strip():
            remote.append(f"{key} set")
    kind = environ.get("CLAUDE_CODE_ENVIRONMENT_KIND", "").strip()
    if kind:
        remote.append(f"CLAUDE_CODE_ENVIRONMENT_KIND={kind}")
    entry = (environ.get("CLAUDE_CODE_ENTRYPOINT", "") or transcript_entrypoint).strip()
    if entry and entry.lower().startswith(REMOTE_ENTRYPOINT_PREFIXES):
        remote.append(f"entrypoint={entry}")
    if remote:
        return EnumAccessMode.REMOTE, tuple(remote)
    if entry in LOCAL_ENTRYPOINTS:
        return EnumAccessMode.LOCAL, (f"entrypoint={entry}",)
    if entry.lower().startswith(SDK_ENTRYPOINT_PREFIXES):
        return EnumAccessMode.HEADLESS, (f"entrypoint={entry}",)
    return None, ((f"entrypoint={entry}",) if entry else ())


class HandlerAccessModeDetect:
    """Resolve the mode the supervision rules use, overrides included."""

    def handle(self, request: ModelAccessModeRequest) -> ModelAccessModeResult:
        detected, signals = detect(request.environ, request.transcript_entrypoint)

        forced = request.environ.get(OVERRIDE_ENV, "").strip().lower()
        if forced in {mode.value for mode in EnumAccessMode}:
            return ModelAccessModeResult(
                EnumAccessMode(forced),
                "env-override",
                detected,
                (f"{OVERRIDE_ENV}={forced}",),
            )
        if detected is EnumAccessMode.HEADLESS:
            return ModelAccessModeResult(detected, "detected", detected, signals)

        session = read_session(request.state_dir, request.session_id)
        word = str(session.get("override", "")).strip().lower()
        if word in {EnumAccessMode.LOCAL.value, EnumAccessMode.REMOTE.value}:
            return ModelAccessModeResult(
                EnumAccessMode(word),
                "session-override",
                detected,
                (f"prompt command mode {word}",),
            )
        word = read_global_override(request.state_dir)
        if word:
            return ModelAccessModeResult(
                EnumAccessMode(word),
                "global-override",
                detected,
                (f"onex-access-mode set {word}",),
            )
        if detected is not None:
            return ModelAccessModeResult(detected, "detected", detected, signals)
        return ModelAccessModeResult(EnumAccessMode.REMOTE, "unknown", None, signals)
