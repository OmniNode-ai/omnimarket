# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_table_copies_drift_compute.runtime_host_table_copies_drift_tick [--probe]``.

The host installer's daily tick form of the copies drift reading. Without ``--probe``, it runs the node's ``daily``
verb. ``--probe`` loads the overlay and prints ``PROBE ok`` without reading a copy or appending a row.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_host_table_copies_drift_compute.overlay import load_overlay
from omnimarket.nodes.node_host_table_copies_drift_compute.runtime_host_table_copies_drift import (
    main as drift_main,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="host-table-copies-drift-tick", description=__doc__
    )
    parser.add_argument("--omni-home", default="")
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.probe:
        try:
            load_overlay()
        except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
            sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
            return 3
        sys.stdout.write("PROBE ok\n")
        return 0
    return drift_main(["daily"])


if __name__ == "__main__":
    raise SystemExit(main())
