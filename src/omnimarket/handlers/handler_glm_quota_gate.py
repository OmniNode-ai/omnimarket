# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hold GLM out of the lane delegation ladders while the Coding Plan's limit is exhausted (OMN-24651).

z.ai answers a call over the plan's weekly or monthly limit with a 429 that carries business code 1310 and the reset
time, after the claude CLI has retried it for about three minutes. Receipt 018b9825-a3b3-4d5d-9338-1b49f4fa47b8 of
2026-11-14 took 188.989 s to read ``claude-glm exited rc=1: API Error: Request rejected (429) . [1310][Weekly/Monthly
Limit Exhausted. Your limit will reset at 2026-11-15 01:38:18][2026111421065430b8c2c98d0a4d56]``. The lane delegation
step then asked glm-5.3, which draws on the same quota, and asked again on the next step of every lane: 58 receipts on
one host carry the same reset.

The reset stamp is Beijing time. The call began 2026-11-14T13:03:45Z and lasted 189 s, and its request id reads
20261114210654, the end of the call plus eight hours. ``parse_reset_instant`` reads the naive stamp as UTC, which would
hold GLM eight hours past the reset, so this gate re-reads the stamp it returns in UTC+8.

The pure handler decides; ``main`` is the adapter the lane delegation step runs (``python -m
omnimarket.handlers.handler_glm_quota_gate check|note <state_root> [error text]``). It keeps the hold in one marker file
under the state root and prints one JSON line. A 429 without code 1310 (a rate limit, code 1302) is transient and sets
no hold. Every failure to read or write the marker reads as not held: the gate is an optimisation and never bricks a
ladder.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from omnimarket.handlers.handler_provider_quota_reset import parse_reset_instant
from omnimarket.models.delegation.model_glm_quota_gate import (
    ModelGlmQuotaGateRequest,
    ModelGlmQuotaGateResult,
)

MARKER_NAME = "glm-quota-exhausted.json"
BEIJING = timezone(timedelta(hours=8))
# A limit notice that states no reset holds GLM out this long.
UNSTATED_HOLD = timedelta(hours=1)
# A marker further out than a monthly plan can run is garbage, not a hold.
LONGEST_HOLD = timedelta(days=35)
_EXHAUSTED = re.compile(r"\[1310\]|Weekly/Monthly Limit Exhausted")


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def exhausted_until(error_text: str, now: datetime) -> datetime | None:
    """When the plan's limit named in ``error_text`` resets (UTC), or None when the text is no exhausted-limit notice."""
    if not _EXHAUSTED.search(error_text or ""):
        return None
    stated = parse_reset_instant(error_text)
    if stated is None:
        return now + UNSTATED_HOLD
    return stated.replace(tzinfo=None).replace(tzinfo=BEIJING).astimezone(UTC)


class HandlerGlmQuotaGate:
    def handle(self, request: ModelGlmQuotaGateRequest) -> ModelGlmQuotaGateResult:
        now = request.now
        until = (
            request.marker_until
            if request.marker_until is None
            or request.marker_until <= now + LONGEST_HOLD
            else None
        )
        write_until = None
        if request.operation == "note":
            reset = exhausted_until(request.error_text, now)
            if reset is not None and reset > now and (until is None or reset > until):
                until = write_until = reset
        if until is None or until <= now:
            return ModelGlmQuotaGateResult(held=False)
        return ModelGlmQuotaGateResult(
            held=True,
            until=until,
            write_until=write_until,
            reason=f"GLM skipped: the Coding Plan's limit is exhausted until {_stamp(until)} (z.ai code 1310); this lane did not call it",
        )


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


def _write_marker(path: Path, until: datetime, noted: datetime) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(
                {"until": _stamp(until), "code": "1310", "noted_at": _stamp(noted)}
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2 or args[0] not in ("check", "note"):
        sys.stdout.write(json.dumps({"held": False, "reason": ""}) + "\n")
        return 2
    path = Path(args[1]) / MARKER_NAME
    at = now or datetime.now(UTC)
    result = HandlerGlmQuotaGate().handle(
        ModelGlmQuotaGateRequest(
            operation=args[0],
            now=at,
            error_text=" ".join(args[2:]),
            marker_until=_read_marker(path),
        )
    )
    if result.write_until is not None:
        _write_marker(path, result.write_until, at)
    sys.stdout.write(
        json.dumps(
            {
                "held": result.held,
                "until": _stamp(result.until) if result.until else "",
                "reason": result.reason,
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
