# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request entry of the undeclared read check: a unified diff on stdin or in a file, the overlay roots by flag.

    python -m omnimarket.nodes.node_undeclared_read_check_compute.runtime_undeclared_read_check --diff-file change.diff --overlays-root <dir>

Exit 0: no undeclared read (or the diff adds no read at all). Exit 1: a read no overlay declares is added.
Exit 3: the diff adds a read and the declared_settings register could not be read from the roots, so the
check refuses to answer rather than guess. Exit 2: a usage error.
"""

from __future__ import annotations

import argparse
import sys

from omnimarket.handlers.overlay_documents_reader import (
    load_overlay_documents,
    read_text_file,
)
from omnimarket.nodes.node_undeclared_read_check_compute.handlers import (
    HandlerUndeclaredReadCheck,
)
from omnimarket.nodes.node_undeclared_read_check_compute.models import (
    ModelUndeclaredReadCheckRequest,
)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_UNREAD = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_undeclared_read_check_compute")
    parser.add_argument(
        "--diff-file", default="-", help="unified diff to judge; - reads stdin"
    )
    parser.add_argument(
        "--overlays-root",
        action="append",
        default=[],
        help="an overlays directory (holding <name>/overlay.yaml); a path list with : is split",
    )
    args = parser.parse_args(argv)
    roots = [part for value in args.overlays_root for part in value.split(":") if part]
    if args.diff_file == "-":
        diff_text = sys.stdin.read()
    else:
        diff_text = read_text_file(args.diff_file)
    handler = HandlerUndeclaredReadCheck()
    first = handler.handle(
        ModelUndeclaredReadCheckRequest(diff_text=diff_text, overlay_documents={})
    )
    if not first.reads:
        sys.stdout.write("undeclared-read-check: the diff adds no read of a setting\n")
        return EXIT_OK
    result = handler.handle(
        ModelUndeclaredReadCheckRequest(
            diff_text=diff_text, overlay_documents=load_overlay_documents(roots)
        )
    )
    if not result.register_present:
        sys.stderr.write(
            f"undeclared-read-check: UNREAD, the diff adds {len(result.reads)} read(s) and no declared_settings "
            f"register was found under {roots or 'any overlays root (none given)'}\n"
        )
        return EXIT_UNREAD
    for read in result.undeclared:
        sys.stderr.write(
            f"undeclared-read-check: REFUSED {read.path}:{read.line} reads {read.kind.value} {read.name}, "
            "which no overlay declares; declare it in overlays/declared_settings/overlay.yaml\n"
        )
    if result.refused:
        return EXIT_REFUSED
    sys.stdout.write(
        f"undeclared-read-check: {len(result.reads)} added read(s), all declared\n"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
