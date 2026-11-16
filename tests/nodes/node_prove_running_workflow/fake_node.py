# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stand-in node command for the workflow tests: records each step key it runs and answers as scripted.

``read``: installed per FAKE_INSTALLED, running from reading number FAKE_RUNNING_AFTER on (0 = never).
``install``: answers installed. ``ledger-append <row>``: appends the row to FAKE_LEDGER (the ledger CLI's place).
Every run appends ``ONEX_RUN_STEP_KEY`` to FAKE_STEP_LOG, so a test counts how often each step ran.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    verb = sys.argv[1]
    key = os.environ["ONEX_RUN_STEP_KEY"]
    with open(os.environ["FAKE_STEP_LOG"], "a", encoding="utf-8") as log:
        log.write(f"{verb} {key}\n")
    if verb == "ledger-append":
        with open(os.environ["FAKE_LEDGER"], "a", encoding="utf-8") as ledger:
            ledger.write(sys.argv[2] + "\n")
        return 0
    step = json.loads(sys.stdin.read())
    if verb == "install":
        print(json.dumps({"installer": "fake", "detail": "installed"}))
        return 0
    number = int(key.rsplit("/", 1)[-1]) if "/read_running/" in key else 0
    after = int(os.environ.get("FAKE_RUNNING_AFTER", "0"))
    running = after > 0 and number >= after
    print(
        json.dumps(
            {
                "installed": os.environ.get("FAKE_INSTALLED", "1") == "1",
                "running": running,
                "host": step["change"]["target"],
                "revision": step["change"]["merge_sha"][:12],
                "observed": "dispatch seen in the runtime log"
                if running
                else "no dispatch yet",
                "detail": f"reading {number}",
                "read_at": f"2026-11-16T16:{number:02d}:00Z",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
