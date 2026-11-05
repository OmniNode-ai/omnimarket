# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A step command for the conformance suite: one JSON ledger row per effect, optionally keyed.

``--idem 1`` makes a second delivery of the same key a no-op (the shape a ledger append with a request key has);
``--idem 0`` appends again, which is what an unkeyed command does under at-least-once delivery. ``--after`` holds the
process after the row is written, the window between "effect done" and "step recorded".
"""

import argparse
import fcntl
import json
import os
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--ledger", required=True)
parser.add_argument("--idem", type=int, default=1)
parser.add_argument("--after", type=float, default=0.0)
parser.add_argument("--exit", type=int, default=0)
args = parser.parse_args()

key = os.environ["ONEX_RUN_STEP_KEY"]
fd = os.open(args.ledger, os.O_RDWR | os.O_CREAT | os.O_APPEND)
fcntl.flock(fd, fcntl.LOCK_EX)
try:
    with open(args.ledger) as existing:
        seen = any(json.loads(line)["key"] == key for line in existing)
    if args.idem and seen:
        sys.stdout.write("skip-existing\n")
        raise SystemExit(args.exit)
    row = {
        "key": key,
        "attempt": os.environ["ONEX_RUN_STEP_ATTEMPT"],
        "pid": os.getpid(),
    }
    os.write(fd, (json.dumps(row) + "\n").encode())
    os.fsync(fd)
finally:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
time.sleep(args.after)
sys.stdout.write("recorded\n")
raise SystemExit(args.exit)
