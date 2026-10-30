# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stand-in for ``onex-ledger`` for the front door tests: ``catchup``, ``inbox`` and ``row CLAIM``.

``row CLAIM`` appends one row to ``$ONEX_LEDGER_PATH`` naming the writer as
``$ONEX_WORK_LEDGER_PRINCIPAL`` and prints ``OK CLAIM <ts> ...``. ``$FAKE_LEDGER_ROW`` changes that:
``refuse`` prints a refusal and exits 65, ``silent`` exits 0 and prints nothing (an unknown
outcome). Every call is appended to ``$FAKE_LEDGER_LOG`` as JSON with the principal it ran as.
"""

from __future__ import annotations

import json
import os
import sys

TS = "2026-10-29T10:00:00Z"


def main() -> int:
    argv = sys.argv[1:]
    log = os.environ.get("FAKE_LEDGER_LOG")
    principal = os.environ.get("ONEX_WORK_LEDGER_PRINCIPAL", "")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": argv, "principal": principal}) + "\n")
    verb = argv[0] if argv else ""
    if verb == "catchup":
        print(f"catchup since={TS} open-claims=2 holds=1 rulings=3")
        return 0
    if verb == "inbox":
        print(f"inbox {argv[1]}: 1 open, 0 need action")
        return 0
    if verb == "row" and argv[1:2] == ["CLAIM"]:
        mode = os.environ.get("FAKE_LEDGER_ROW", "ok")
        if mode == "refuse":
            print("REFUSED 65 attribution: the bus refused the append")
            return 65
        if mode == "silent":
            return 0
        lane = argv[argv.index("--lane") + 1]
        with open(os.environ["ONEX_LEDGER_PATH"], "a", encoding="utf-8") as fh:
            fh.write(f"{TS} | CLAIM | lane={lane} | principal={principal} | fixture\n")
        print(f"OK CLAIM {TS} lane={lane}")
        return 0
    print(f"REFUSED 64 usage: unknown verb {verb!r}")
    return 64


if __name__ == "__main__":
    sys.exit(main())
