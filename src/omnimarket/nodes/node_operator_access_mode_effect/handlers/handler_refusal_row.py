# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Write one refusal row for a refused supervision check (OMN-21020).

A supervision hook that refuses an action told the model on stderr, or in a Stop block, and left
nothing the morning friction sweep reads. This calls the refusal recorder's shell entry,
``hook_refusal.sh`` in omniclaude's hook library, which every omniclaude guard already records
through: the recorder resolves the lane from the payload's facts, redacts, dedupes and carries the
suppressed count, and appends the ``class=refusal`` row through the locked ledger writer. Dedupe and
suppression stay the recorder's; this passes the gate's own ``guard=`` and ``reason=`` and stops.

The installed hooks run from a copy of this node with the standard library alone, so they cannot
import omniclaude. They call the shell entry instead, found at ``ONEX_HOOK_REFUSAL_SEAM`` or, under
the registry root (``OMNI_HOME``, else ``ONEX_REGISTRY_ROOT``), at
``omniclaude/plugins/onex/hooks/lib/hook_refusal.sh``.

Fail-open by construction: a recorder that cannot be found or run never changes the refusal the
caller has already decided. The payload handed over names the session, transcript, working
directory and agent, the facts that locate a lane, and never the tool call's input.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SEAM_RELATIVE = (
    Path("omniclaude") / "plugins" / "onex" / "hooks" / "lib" / "hook_refusal.sh"
)
SEAM_ENV = "ONEX_HOOK_REFUSAL_SEAM"
REGISTRY_ENVS = ("OMNI_HOME", "ONEX_REGISTRY_ROOT")
PAYLOAD_KEYS = (
    "session_id",
    "transcript_path",
    "cwd",
    "agent_id",
    "tool_name",
    "hook_event_name",
)
#: The seam backgrounds the recorder itself, so this only waits for the shell that starts it.
START_TIMEOUT_SECONDS = 10
#: The recorder redacts and truncates the detail again; this only bounds the argument.
DETAIL_LIMIT = 400

INTERPRETER_ENV = frozenset({"PYTHONPATH", "PYTHONHOME"})

SOURCE_AND_RECORD = 'source "$1" || exit 0; hook_record_refusal "$2" "$3"'


def seam_path(environ: Mapping[str, str]) -> Path | None:
    """The recorder's shell entry, or None when this host names no registry root."""
    declared = environ.get(SEAM_ENV, "")
    if declared:
        candidate = Path(declared)
        return candidate if candidate.is_file() else None
    for name in REGISTRY_ENVS:
        root = environ.get(name, "")
        if root and Path(root).is_absolute():
            candidate = Path(root) / SEAM_RELATIVE
            if candidate.is_file():
                return candidate
    return None


def reduced_payload(payload: Mapping[str, Any]) -> str:
    """The lane-locating facts of a hook payload as JSON; the tool input is left out."""
    kept = {
        key: payload[key] for key in PAYLOAD_KEYS if isinstance(payload.get(key), str)
    }
    return json.dumps(kept)


def record_refusal(
    guard: str,
    reason_class: str,
    reason: str,
    payload: Mapping[str, Any],
    environ: Mapping[str, str],
) -> bool:
    """Ask the recorder for one row; True when the shell entry ran, False when it could not."""
    seam = seam_path(environ)
    if seam is None or not guard or not reason_class:
        return False
    # The installed command line sets PYTHONPATH to the stub copy of this node. Passed on, it
    # shadows the ledger writer's own package for the writer the recorder starts, and the
    # row is never written; omniclaude's own entrypoint unsets it for the same reason.
    env = {k: v for k, v in environ.items() if k not in INTERPRETER_ENV}
    env["_OMNICLAUDE_HOOK_NAME"] = guard
    env["_OMNICLAUDE_HOOK_PAYLOAD"] = reduced_payload(payload)
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        env["CLAUDE_PROJECT_DIR"] = cwd
    registry = next((environ[n] for n in REGISTRY_ENVS if environ.get(n)), "")
    if registry:
        env.setdefault("ONEX_REGISTRY_ROOT", registry)
    detail = reason.splitlines()[0][:DETAIL_LIMIT] if reason.strip() else ""
    try:
        completed = subprocess.run(
            ["bash", "-c", SOURCE_AND_RECORD, "bash", str(seam), reason_class, detail],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=START_TIMEOUT_SECONDS,
            check=False,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0
