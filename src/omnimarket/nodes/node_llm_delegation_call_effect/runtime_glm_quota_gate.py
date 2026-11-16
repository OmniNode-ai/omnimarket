# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_llm_delegation_call_effect.runtime_glm_quota_gate check|note <state_root> [error text]``.

The command line the lane delegation step runs before and after each GLM call (OMN-24651). It keeps the hold in one
marker file under the state root and prints one JSON line: ``{"held", "until", "reason"}``. The contract's
``glm_quota_gate`` block says why the hold lives in that file and not in ``provider_quota_state``.

The gate is an optimisation and never bricks a ladder: every failure to read or write the marker, or to load the quota
policy, reads as not held, and the exit code is 0 for any well-formed call.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.handler_glm_quota_gate import HandlerGlmQuotaGate
from omnimarket.models.delegation.model_glm_quota_gate import (
    ModelGlmQuotaGateRequest,
)

MARKER_NAME = "glm-quota-exhausted.json"
EXIT_INVALID = 2
_NOT_HELD = {"held": False, "until": "", "reason": ""}


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_marker(path: Path) -> datetime | None:
    try:
        until = datetime.fromisoformat(
            str(json.loads(path.read_text(encoding="utf-8"))["until"]).replace(
                "Z", "+00:00"
            )
        )
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return None
    return until if until.tzinfo is not None else None


def _write_marker(path: Path, until: datetime, noted: datetime, code: str) -> bool:
    """Persist the hold; False when the state root cannot take it."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(
                {"until": _stamp(until), "code": code, "noted_at": _stamp(noted)}
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        # The marker is best effort: a state root that cannot be written reads as not held.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return False
    return True


def _answer(args: Sequence[str], at: datetime) -> dict[str, object]:
    path = Path(args[1]) / MARKER_NAME
    result = HandlerGlmQuotaGate().handle(
        ModelGlmQuotaGateRequest(
            operation=args[0],
            now=at,
            error_text=" ".join(args[2:]),
            marker_until=_read_marker(path),
        )
    )
    if result.write_until is not None and not _write_marker(
        path, result.write_until, at, result.code
    ):
        return dict(_NOT_HELD)
    return {
        "held": result.held,
        "until": _stamp(result.until) if result.until else "",
        "reason": result.reason,
    }


def main(argv: Sequence[str] | None = None, now: datetime | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or args[0] not in ("check", "note"):
        sys.stdout.write(json.dumps(_NOT_HELD) + "\n")
        return EXIT_INVALID
    try:
        answer = _answer(args, now or datetime.now(UTC))
    except Exception:
        # Fail open for any failure (a policy loader that raises AttributeError included): the gate is an
        # optimisation and must always print its JSON line, never a traceback.
        answer = dict(_NOT_HELD)
    sys.stdout.write(json.dumps(answer) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
