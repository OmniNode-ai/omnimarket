# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22848 seam: what the floor fill's start supplies against what this node's entry requires.

The fill (``node_floor_fill_orchestrator``, lane fillorch) starts a lane by filling the four
placeholders ``{lane} {key} {ticket} {attempt}`` into ``floor_fill.runner_command`` and running the
result. This entry takes its flags from ``runtime_remote_lane_start.parser``. Both sides are read
from their source here (the fill's example overlay, the entry's own parser), so the test fails when
either side changes shape:

* every flag the fill's command passes must be one the entry's parser accepts (a renamed flag fails);
* the flags the entry requires and the fill does not supply are pinned in ``UNBRIDGED_REQUIRED_FLAGS``
  (the measured gap: a fill-started lane cannot reach the entry until each is supplied by the fill's
  command or by an overlay key). A flag becoming supplied, or a new required flag, fails the pin.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_remote_lane_start_orchestrator.runtime_remote_lane_start import (
    parser,
)

pytestmark = [pytest.mark.unit]

NODES = Path(__file__).resolve().parents[2]
FILL_OVERLAY = NODES / "node_floor_fill_orchestrator" / "overlay.example.yaml"
FILL_PLACEHOLDERS = {
    "lane": "floor-alpha-a1",
    "key": "floor:alpha:alpha-t1",
    "ticket": "OMN-22848",
    "attempt": "1",
}
#: Measured 2026-11-10 against omnimarket dev: the fill supplies lane, key, ticket and attempt;
#: these are required by the entry and supplied by neither the fill's placeholders nor an overlay key.
UNBRIDGED_REQUIRED_FLAGS = frozenset(
    {"--overlay", "--ledger", "--pool-snapshot", "--model", "--brief", "--run-id"}
)


def fill_command() -> list[str]:
    overlay = yaml.safe_load(FILL_OVERLAY.read_text(encoding="utf-8"))
    return [
        re.sub(r"\{(\w+)\}", lambda m: FILL_PLACEHOLDERS[m.group(1)], part)
        for part in overlay["floor_fill"]["runner_command"]
    ]


def entry_required_flags(capsys: pytest.CaptureFixture[str]) -> frozenset[str]:
    """The flags the parser demands, read from its own refusal of an empty command line."""
    with pytest.raises(SystemExit):
        parser().parse_args([])
    refusal = capsys.readouterr().err
    listed = refusal.split("the following arguments are required:")[1]
    return frozenset(flag.strip() for flag in listed.split(","))


def entry_known_flags() -> frozenset[str]:
    return frozenset(re.findall(r"--[a-z][a-z-]*", parser().format_help()))


def supplied_flags(command: list[str]) -> frozenset[str]:
    return frozenset(part for part in command if part.startswith("--"))


def test_fill_seam_every_flag_the_fill_passes_is_accepted_by_the_entry() -> None:
    assert supplied_flags(fill_command()) <= entry_known_flags()


def test_fill_seam_the_fill_fills_exactly_the_four_placeholders() -> None:
    overlay = yaml.safe_load(FILL_OVERLAY.read_text(encoding="utf-8"))
    placeholders = set(
        re.findall(r"\{(\w+)\}", " ".join(overlay["floor_fill"]["runner_command"]))
    )
    assert placeholders == set(FILL_PLACEHOLDERS)


def test_fill_seam_the_flags_the_fill_does_not_supply_are_the_pinned_gap(
    capsys: pytest.CaptureFixture[str],
) -> None:
    unbridged = entry_required_flags(capsys) - supplied_flags(fill_command())
    assert unbridged == UNBRIDGED_REQUIRED_FLAGS, (
        "the entry's required flags the fill does not supply changed: "
        f"now {sorted(unbridged)}"
    )
