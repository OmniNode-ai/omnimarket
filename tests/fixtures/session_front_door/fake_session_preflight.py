# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stand-in for the onex plugin's session preflight runner, for the front door tests.

It keeps the runner's contract the front door depends on: ``--profile operator|team`` (default
``operator``) runs every check except those tagged with another profile, ``command`` checks run
through ``bash -c`` in the caller's environment, and ``--receipt`` gets the JSON verdict with the
profile, overlay path and hash, and the included, skipped, passed and failed check ids. Every argv
it is called with is appended to ``$FAKE_PREFLIGHT_LOG`` when that is set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--intent", default="normal")
    parser.add_argument("--receipt")
    parser.add_argument("--overlay")
    parser.add_argument("--profile", choices=("operator", "team"), default="operator")
    args = parser.parse_args()
    log = os.environ.get("FAKE_PREFLIGHT_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(sys.argv[1:]) + "\n")
    overlay = Path(args.overlay or os.environ["SESSION_PREFLIGHT_OVERLAY_PATH"])
    raw = overlay.read_bytes()
    checks = yaml.safe_load(raw)["checks"]
    run = [c for c in checks if c.get("profile", args.profile) == args.profile]
    skipped = [c["check_id"] for c in checks if c not in run]
    results = []
    for check in run:
        done = subprocess.run(
            ["bash", "-c", check["command"]],
            capture_output=True,
            text=True,
            check=False,
        )
        ok = done.returncode == 0
        results.append(
            {
                "check_id": check["check_id"],
                "title": check["title"],
                "kind": check["kind"],
                "severity": check.get("severity", "blocker"),
                "fix": check["fix"],
                "ok": ok,
                "detail": "" if ok else f"exit {done.returncode}",
            }
        )
    blocked = any(not r["ok"] and r["severity"] == "blocker" for r in results)
    if args.receipt:
        Path(args.receipt).write_text(
            json.dumps(
                {
                    "intent": args.intent,
                    "verdict": "BLOCKED" if blocked else "OK",
                    "passed": sum(1 for r in results if r["ok"]),
                    "total": len(results),
                    "checks": results,
                    "profile": args.profile,
                    "overlay_path": str(overlay),
                    "overlay_sha256": hashlib.sha256(raw).hexdigest(),
                    "included_check_ids": [r["check_id"] for r in results],
                    "skipped_check_ids": skipped,
                    "passed_check_ids": [r["check_id"] for r in results if r["ok"]],
                    "failed_check_ids": [r["check_id"] for r in results if not r["ok"]],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
