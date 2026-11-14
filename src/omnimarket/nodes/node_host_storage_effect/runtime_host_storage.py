# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one declared storage command from the command line, against the overlay's backend (OMN-22906)."""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_host_storage_effect.handlers.handler_host_storage import (
    HandlerHostStorage,
    select_backend,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    EnumHostStorageOutcome,
    ModelHostStorageRequest,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage_overlay import (
    NODE_NAME,
    ModelHostStorageOverlay,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--requested-by", required=True)
    # Entries come from a file (one absolute path per line), never the command line: the holder scan
    # reads every process's command line, so an entry named there would read as held by this process.
    parser.add_argument("--entries-file", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        entries = (
            tuple(
                line
                for line in args.entries_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            if args.entries_file
            else ()
        )
        request = ModelHostStorageRequest(
            host=args.host,
            command=args.command,
            requested_by=args.requested_by,
            entries=entries,
        )
        overlay = load_structured_overlay(NODE_NAME, ModelHostStorageOverlay)
        result = HandlerHostStorage(overlay, select_backend(overlay)).handle(request)
    except (ValidationError, NodeOverlayError, OSError) as exc:
        sys.stderr.write(f"host-storage unavailable: {type(exc).__name__}\n")
        return 4
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    if result.outcome is EnumHostStorageOutcome.OK:
        return 0
    if result.outcome is EnumHostStorageOutcome.REFUSED:
        return 2
    if result.outcome is EnumHostStorageOutcome.COMPLETED_UNOBSERVED:
        return 5
    if result.outcome is EnumHostStorageOutcome.PARTIAL:
        return 6
    return 3


if __name__ == "__main__":
    sys.exit(main())
