# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stand-in for the node's commands: ``plan`` names one due owner, ``place`` counts placements, ``read`` exits 1.

Each call appends ``<command> <step key>`` to the file named by ``FAKE_ITEM_OWNER_LOG`` so a test counts the steps.
"""

from __future__ import annotations

import json
import os
import sys

DUE = {
    "item": {
        "item_id": "itm-0123456789",
        "source": "blocked-lane",
        "source_ref": "2026-11-19T11:00:00Z fix-a-1000",
        "first_seen_at": "2026-11-19T11:00:00Z",
        "revision": "abcdefabcdef",
        "state": "open",
        "accountable_owner": None,
        "running_lane": None,
        "generation": 0,
        "unowned_since": "2026-11-19T11:00:00Z",
        "ticket": "OMN-500",
        "pr": None,
        "summary": "blocked",
    },
    "lane": "own-0123456789-g1",
    "generation": 1,
    "reservation_key": "itm-0123456789-rabcdefabcdef-g1",
}


def main() -> int:
    command = sys.argv[1]
    with open(os.environ["FAKE_ITEM_OWNER_LOG"], "a", encoding="utf-8") as log:
        log.write(f"{command} {os.environ.get('ONEX_RUN_STEP_KEY', '')}\n")
    if command == "plan":
        due = dict(
            DUE,
            reservation_key=f"{DUE['reservation_key']}{os.environ.get('FAKE_ITEM_OWNER_KEY', '')}",
        )
        print(json.dumps({"items": [DUE["item"]], "due": [due], "skipped": []}))
        return 0
    if command == "place":
        due = json.loads(sys.stdin.read())
        print(
            json.dumps(
                {
                    "item_id": due["item"]["item_id"],
                    "lane": due["lane"],
                    "generation": due["generation"],
                    "started": True,
                    "owned_row": "row",
                    "detail": "placed",
                }
            )
        )
        return 0
    print("item-unowned verdict=FINDING unowned=1")
    return 1


if __name__ == "__main__":
    sys.exit(main())
