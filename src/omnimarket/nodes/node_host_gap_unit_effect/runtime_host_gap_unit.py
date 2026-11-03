# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one host gap installer unit for one declared host; plans only unless --apply."""

import argparse
import sys
from pathlib import Path

from omnimarket.handlers.handler_host_profile import (
    parse_host_profile,
)
from omnimarket.handlers.handler_host_shell import (
    HandlerHostShell,
)
from omnimarket.models.model_host_profile import EnumGapUnit, ModelHostGapUnitRequest
from omnimarket.nodes.node_host_gap_unit_effect.handlers.handler_host_gap_unit import (
    HandlerHostGapUnit,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--unit", required=True, choices=[u.value for u in EnumGapUnit])
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument(
        "--apply", action="store_true", help="carry out the plan; default is a dry run"
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="measure and act on this host, not over ssh",
    )
    args = parser.parse_args(argv)
    profile = parse_host_profile(args.profiles.read_text(), args.host)
    if args.local:
        profile = profile.model_copy(update={"ssh_target": None})
    adapter = HandlerHostShell()
    result = HandlerHostGapUnit(adapter, adapter).handle(
        ModelHostGapUnitRequest(
            profile=profile, unit=EnumGapUnit(args.unit), apply=args.apply
        )
    )
    mode = "APPLIED" if result.applied else "DRY-RUN"
    for action in result.planned:
        sys.stdout.write(
            f"{mode} {result.host} {result.unit.value} {action.kind.value} {action.path}"
            + "\n"
        )
    sys.stdout.write(
        f"{mode} {result.host} {result.unit.value}: planned={len(result.planned)} done={len(result.done)}"
        + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
