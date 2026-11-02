# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the runtime inventory check: ``python -m omnimarket.nodes.node_work_ledger_runtime_inventory_effect.runtime_inventory_check``.

Prints ``PASS rows=<n> exclusions=<n> observed=<n>`` or ``FAIL`` with one finding per line, and
exits 0 or 1. The overlay is named by ``--inventory``, ``--overlay-root`` or the environment; there
is no default location.
"""

from __future__ import annotations

import argparse
import sys

from omnimarket.nodes.node_work_ledger_runtime_inventory_effect.handlers.handler_runtime_inventory import (
    HandlerRuntimeInventory,
)
from omnimarket.nodes.node_work_ledger_runtime_inventory_effect.models.model_runtime_inventory import (
    ModelRuntimeInventoryRequest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="work-ledger-runtime-inventory-check")
    parser.add_argument("--inventory", default=None, help="inventory overlay file")
    parser.add_argument("--snapshot", default=None, help="recorded host snapshot file")
    parser.add_argument(
        "--overlay-root", action="append", default=[], help="overlay root directory"
    )
    args = parser.parse_args(argv)
    result = HandlerRuntimeInventory().handle(
        ModelRuntimeInventoryRequest(
            inventory_path=args.inventory,
            snapshot_path=args.snapshot,
            overlay_roots=list(args.overlay_root),
        )
    )
    if result.verdict == "PASS":
        sys.stdout.write(
            f"PASS rows={result.row_count} exclusions={result.exclusion_count} observed={result.observed_count}\n"
        )
        return 0
    sys.stdout.write("FAIL\n")
    for finding in result.findings:
        sys.stdout.write(f"  {finding}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
