# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A recorded stand-in for the fill node's commands and the ledger CLI, for the workflow 2 tests.

``pool``: free slots FAKE_FREE_SLOTS, unowned work FAKE_UNOWNED; with FAKE_UNREAD set, it answers what the real fill
prints for a source it could not read (a failed status, the cause in ``unread``, exit 4) for ``pool`` and ``fill`` alike.
While FAKE_HANG names an existing file, ``fill`` sleeps (a step in flight when its worker goes away). ``fill``: places the lanes in FAKE_PLACED (comma
list; empty places none, with the reason FAKE_WHY) and, for each lane in FAKE_CLAIMS, appends that lane's CLAIM row to
FAKE_LEDGER as the placed lane would. ``ledger-read``: prints FAKE_LEDGER. ``ledger-append <row>``: appends the row.
Every run appends ``<verb> ONEX_RUN_STEP_KEY`` to FAKE_STEP_LOG, so a test counts how often each step ran.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _done(verb: str, key: str) -> None:
    """Logged when a step finished, so a test tells a step that ran from one that was cut short."""
    with open(os.environ["FAKE_STEP_LOG"], "a", encoding="utf-8") as log:
        log.write(f"done-{verb} {key}\n")


def main() -> int:
    verb = sys.argv[1]
    key = os.environ.get("ONEX_RUN_STEP_KEY", "none")
    with open(os.environ["FAKE_STEP_LOG"], "a", encoding="utf-8") as log:
        log.write(f"{verb} {key}\n")
    ledger = Path(os.environ["FAKE_LEDGER"])
    if verb == "ledger-append":
        with ledger.open("a", encoding="utf-8") as out:
            out.write(sys.argv[2] + "\n")
        return 0
    if verb == "ledger-read":
        sys.stdout.write(ledger.read_text(encoding="utf-8") if ledger.exists() else "")
        return 0
    sys.stdin.read()
    unread = os.environ.get("FAKE_UNREAD", "")
    if unread and verb in ("pool", "fill"):
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "exit_code": 4,
                    "free_slots": None,
                    "unowned_work": None,
                    "placed_lanes": [],
                    "unread": unread,
                    "why_none": f"UNKNOWN {unread}",
                }
            )
        )
        _done(verb, key)
        return 4
    hang = os.environ.get("FAKE_HANG", "")
    if verb == "fill" and hang and Path(hang).exists():
        time.sleep(3600)
    if verb == "pool":
        free = int(os.environ.get("FAKE_FREE_SLOTS", "0"))
        unowned = int(os.environ.get("FAKE_UNOWNED", "0"))
        print(
            json.dumps({"free_slots": free, "unowned_work": unowned, "read_at": _now()})
        )
        _done(verb, key)
        return 0
    placed = [lane for lane in os.environ.get("FAKE_PLACED", "").split(",") if lane]
    stamp = _now()
    claims = set(os.environ.get("FAKE_CLAIMS", "").split(","))
    with ledger.open("a", encoding="utf-8") as out:
        for lane in placed:
            if lane in claims:
                out.write(
                    f"{stamp} | CLAIM | lane={lane} | ticket=OMN-1 | placed lane claims\n"
                )
    print(
        json.dumps(
            {
                "placed_lanes": placed,
                "why_none": ""
                if placed
                else os.environ.get("FAKE_WHY", "no unowned work"),
                "read_at": stamp,
            }
        )
    )
    _done(verb, key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
