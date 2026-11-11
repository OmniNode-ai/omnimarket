# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22848 seam: what the floor fill's start supplies against what this node's entry requires.

The fill (``node_floor_fill_orchestrator``, lane fillorch) starts a lane by filling the five
placeholders ``{lane} {key} {ticket} {attempt} {brief}`` into ``floor_fill.runner_command`` (the brief is the file
the fill writes for the line's task) and running the result; the command carries the entry's other required
flags as fixed elements. This entry takes its flags from ``runtime_remote_lane_start.parser``. Both sides are read
from their source here (the fill's example overlay, the entry's own parser), so the test fails when
either side changes shape:

* every flag the fill's command passes must be one the entry's parser accepts (a renamed flag fails);
* the flags the entry requires and the fill does not supply are pinned in ``UNBRIDGED_REQUIRED_FLAGS`` (empty: measured
  2026-11-10 as six, the reason a floor-line start ended in the parser's exit 2; the fill's command now carries each). A new
  required flag the fill does not supply fails the pin.
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
    "brief": "/srv/example/briefs/floor-alpha-a1.md",
}
#: Measured 2026-11-10 against omnimarket dev, the fill supplied lane, key, ticket and attempt, and the entry's parser
#: refused a floor-line start (exit 2) for the six flags it requires that the command did not carry: --overlay --ledger
#: --pool-snapshot --model --brief --run-id. The command carries them now; a flag the fill drops comes back here.
UNBRIDGED_REQUIRED_FLAGS: frozenset[str] = frozenset()


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


def test_fill_seam_the_fill_fills_exactly_the_five_placeholders() -> None:
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
