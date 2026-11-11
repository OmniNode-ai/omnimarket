# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_pilot_measurement_effect.runtime_pilot_measurement_tick [--probe]``.

The host installer's tick form of the hourly reading (OMN-23619): one pass measures every declared pilot against its
frozen bar and appends the piece 16 row, exactly as ``python -m omnimarket.nodes.node_pilot_measurement_effect daily``
does. ``--probe`` loads the overlay and prints ``PROBE ok``: it reads no result, builds no appender and writes nothing
(the installer runs a probe before it loads the unit). ``--omni-home`` and ``--state-dir`` are the installer's tick
arguments; the reading takes its settings from the overlay and its environment, not from them.

Exit codes are the node's own: 0 the row was written, 1 the run or its row failed, 3 a deployment fact is missing.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_pilot_measurement_effect.overlay import load_overlay
from omnimarket.nodes.node_pilot_measurement_effect.runtime_pilot_measurement import (
    main as measurement_main,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pilot-measurement-tick", description=__doc__)
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
    return measurement_main(["daily"])


if __name__ == "__main__":
    raise SystemExit(main())
