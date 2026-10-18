# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide what a session start prints from the cached capability digest.

The catalog timer writes ``digest.txt`` (and ``failure.json`` when a run fails) under
``<state_dir>/capability``. This handler only reads those two files: it opens no socket and runs no
probe. Inside a registry root it prints the digest, or a STALE line with its fix when the digest is
missing or older than the freshness bound; on a compact restart it prints one pointer line; outside
every registry root it prints nothing, so an unrelated session is never taxed.
"""

import json
import time
from pathlib import Path

from omnimarket.nodes.node_session_capability_digest_effect.models import (
    EnumSessionCapabilityDigestOutcome,
    ModelSessionCapabilityDigestRequest,
    ModelSessionCapabilityDigestResult,
)

CAPABILITY_DIR = "capability"
DIGEST_FILE = "digest.txt"
FAILURE_FILE = "failure.json"
STALE_AFTER_MINUTES = 45
COMPACT_POINTER = "Capability digest: run `onex-capability-catalog digest`."
LAUNCHD_LABEL = "ai.omninode.capability-catalog"


def _inside_a_root(cwd: str, registry_roots: str) -> bool:
    try:
        here = Path(cwd).resolve()
    except (OSError, RuntimeError):
        return False
    for entry in registry_roots.split(":"):
        if not entry.strip() or not Path(entry).is_absolute():
            continue
        try:
            here.relative_to(Path(entry).resolve())
        except (ValueError, OSError, RuntimeError):
            continue
        return True
    return False


def _failure_reason(capability_dir: Path) -> str:
    try:
        raw = json.loads((capability_dir / FAILURE_FILE).read_text(encoding="utf-8"))
        reason = str(raw["reason"]) if isinstance(raw, dict) else ""
    except (OSError, ValueError, KeyError):
        return ""
    return " ".join(reason.split())


class HandlerSessionCapabilityDigest:
    """Effect handler: read the cached digest and choose the session-start text."""

    def handle(
        self, request: ModelSessionCapabilityDigestRequest
    ) -> ModelSessionCapabilityDigestResult:
        if not _inside_a_root(request.cwd, request.registry_roots):
            return ModelSessionCapabilityDigestResult(
                outcome=EnumSessionCapabilityDigestOutcome.SILENT_OUTSIDE_ROOTS,
                output="",
            )
        if request.source == "compact":
            return ModelSessionCapabilityDigestResult(
                outcome=EnumSessionCapabilityDigestOutcome.COMPACT_POINTER,
                output=COMPACT_POINTER,
            )
        now = time.time() if request.now_epoch is None else request.now_epoch
        capability_dir = Path(request.state_dir) / CAPABILITY_DIR
        digest_path = capability_dir / DIGEST_FILE
        age: int | None = None
        digest = ""
        if request.state_dir:
            try:
                digest = digest_path.read_text(encoding="utf-8")
                age = max(0, int((now - digest_path.stat().st_mtime) // 60))
            except OSError:
                digest = ""
                age = None
        if digest.strip() and age is not None and age <= STALE_AFTER_MINUTES:
            return ModelSessionCapabilityDigestResult(
                outcome=EnumSessionCapabilityDigestOutcome.DIGEST,
                output=digest.rstrip("\n"),
                age_minutes=age,
            )
        lines = [
            f"CAPABILITY DIGEST STALE age={'unknown' if age is None else f'{age}m'}"
        ]
        reason = _failure_reason(capability_dir) if request.state_dir else ""
        if not request.state_dir:
            reason = "the state directory is not set in this environment"
        if reason:
            lines.append(f"reason: {reason}")
        lines.append(f"fix: launchctl kickstart -k gui/{request.uid}/{LAUNCHD_LABEL}")
        return ModelSessionCapabilityDigestResult(
            outcome=EnumSessionCapabilityDigestOutcome.STALE,
            output="\n".join(lines),
            age_minutes=age,
        )
