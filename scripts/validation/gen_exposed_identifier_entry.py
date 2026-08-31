#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17320: mint one exposed_identifiers_denylist.json entry from a literal.

Reads the literal from STDIN, never from argv -- an argv value lands in shell
history, process listings, and any transcript of the session that minted it, which
for a not-yet-leaked identifier would be a fresh disclosure in three more places.

Emits ONLY the digest object. The plaintext is never echoed and never written to
disk by this script; record it in the owning Linear ticket, which is private.

    printf '%s' '<literal>' | python3 scripts/validation/gen_exposed_identifier_entry.py \\
        --id omn-XXXXX-what-it-is --kind tenant-slug --ticket OMN-XXXXX \\
        --notes "where it leaked and what replaced it"

Paste the printed object into the "entries" array, keeping the array sorted by id,
then run the gate to confirm the tree is clean before committing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

DEFAULT_DENYLIST = Path(__file__).with_name("exposed_identifiers_denylist.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--id", required=True, help="stable entry id, e.g. omn-17288-tenant-slug"
    )
    parser.add_argument(
        "--kind", required=True, help="class label, e.g. tenant-slug / tenant-uuid"
    )
    parser.add_argument("--ticket", required=True, help="owning ticket, e.g. OMN-17288")
    parser.add_argument(
        "--notes", required=True, help="where it leaked and what replaced it"
    )
    parser.add_argument(
        "--denylist",
        type=Path,
        default=DEFAULT_DENYLIST,
        help="read the salt from this denylist so the digest is comparable",
    )
    args = parser.parse_args(argv)

    literal = sys.stdin.read().strip()
    if not literal:
        print("ERROR: no literal on stdin", file=sys.stderr)
        return 2
    if len(literal) < 4:
        print(
            "ERROR: literal shorter than 4 chars would match everywhere",
            file=sys.stderr,
        )
        return 2

    salt = json.loads(args.denylist.read_text(encoding="utf-8"))["salt"]
    digest = hashlib.sha256((salt + literal.lower()).encode("utf-8")).hexdigest()

    print(
        json.dumps(
            {
                "id": args.id,
                "kind": args.kind,
                "length": len(literal),
                "sha256": digest,
                "ticket": args.ticket,
                "notes": args.notes,
            },
            indent=6,
        )
    )
    print(
        f"\nminted len={len(literal)} -- plaintext NOT echoed; record it in {args.ticket}.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
