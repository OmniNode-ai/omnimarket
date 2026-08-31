#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# ci_bus_lanes.py
#
# The single home for CI-publisher bus-lane resolution (OMN-14801, OMN-17378).
#
# WHY A SHARED MODULE: this is a FAIL-CLOSED policy surface, not a convenience
# helper. It decides (a) which broker a thin CI publisher targets and (b) whether
# an unresolvable broker is an expected fork-runner skip or a red defect. Two
# private copies of that decision WILL diverge, and a divergence in a fail-closed
# gate is exactly how a publisher goes green-but-silent -- the OMN-17378 class.
#
# CURRENT CONSUMERS: `publish_pr_merged_event.py` only.
# `publish_occ_autobind_command.py` still carries its own private copy of this
# logic (`_MODE_*`, `_resolve_lane_broker`, `_is_trusted_runner`). Porting it
# here is DELIBERATELY out of scope for OMN-17378: that publisher is live,
# gated and working, its module-level private names are referenced by 78 tests
# in tests/unit/nodes/node_occ_companion_effect/, and folding it into this
# change would expand the blast radius of an urgent outage fix onto the one
# publisher that is NOT broken. The consolidation is tracked as its own
# follow-up on OMN-17378 so the duplication is recorded rather than silent.
#
# IMPORTABLE FROM A THIN CI SCRIPT: this is a sibling module in `scripts/`, NOT a
# package import. `python scripts/<publisher>.py` puts `scripts/` on sys.path[0],
# so `from ci_bus_lanes import ...` resolves without executing any omnimarket
# package `__init__` (which would drag in omnibase_core, absent from the minimal
# publisher CI env -- see the `_load_*_topic` docstrings in both publishers).
# Runtime deps: pyyaml + click only.
#
# Ticket: OMN-14801, OMN-17378

from __future__ import annotations

import os
import sys
from pathlib import Path

import click
import yaml

# Checked-in lane -> bus-broker overlay. Resolved relative to this module so the
# resolution is machine-portable (no hardcoded absolute paths).
LANE_OVERLAY_PATH = Path(__file__).resolve().parents[1] / "config" / "ci_bus_lanes.yaml"

# Resolution modes returned by resolve_lane_broker.
MODE_NO_LANE = "no-lane"  # --lane was not supplied
MODE_UNKNOWN_LANE = "unknown-lane"  # --lane supplied but absent from the overlay
MODE_INMEMORY = "inmemory"  # in-process bus (contract-as-data default): no-op
MODE_FROM_SECRET = "from-secret"  # trust the injected secret verbatim (no check)
MODE_CONCRETE = "concrete"  # overlay declares a concrete host:port (preferred)


def load_lane_overlay(path: Path | None = None) -> dict[str, object]:
    """Load the checked-in lane->broker overlay (OMN-14801).

    Returns an empty dict when the overlay file is absent so callers can apply
    the documented "no overlay -> in-memory default" fallback. A malformed
    (non-mapping) overlay is likewise treated as empty rather than crashing the
    thin publisher.
    """
    overlay_path = path if path is not None else LANE_OVERLAY_PATH
    if not overlay_path.exists():
        return {}
    with overlay_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        return {}
    return loaded


def resolve_lane_broker(
    overlay: dict[str, object], lane: str | None
) -> tuple[str, str]:
    """Resolve a CI lane id to ``(mode, declared_broker)`` from the overlay.

    ``mode`` is one of the ``MODE_*`` constants. ``declared_broker`` is the
    concrete ``host:port`` only for ``MODE_CONCRETE``; it is ``""`` otherwise.
    The precedence intentionally mirrors the overlay's documented semantics:
    no lane / unknown lane -> caller applies the in-memory default (or fails
    loud on the trusted runner); an explicit ``inmemory`` -> in-process no-op;
    ``from-secret`` -> trust the injected secret; anything else -> a concrete,
    overlay-preferred broker that the injected secret is checked against.
    """
    if lane is None or not lane.strip():
        return (MODE_NO_LANE, "")
    lane_key = lane.strip()
    lanes_obj = overlay.get("lanes")
    lanes = lanes_obj if isinstance(lanes_obj, dict) else {}
    if lane_key not in lanes:
        return (MODE_UNKNOWN_LANE, "")
    entry = lanes[lane_key]
    broker_raw = entry.get("broker") if isinstance(entry, dict) else entry
    broker_val = "" if broker_raw is None else str(broker_raw).strip()
    if not broker_val or broker_val == MODE_INMEMORY:
        return (MODE_INMEMORY, "")
    if broker_val == MODE_FROM_SECRET:
        return (MODE_FROM_SECRET, "")
    return (MODE_CONCRETE, broker_val)


def is_trusted_runner() -> bool:
    """Return whether this run is on the trusted self-hosted lane (OMN-14451).

    Mirrors the same fork/non-fork test the workflow uses to pick `runs-on:`,
    threaded through as ``RUNNER_IS_TRUSTED`` so a publisher can tell an
    expected fork-runner skip (no broker provisioned, ubuntu-latest) apart
    from a real misconfiguration on the trusted runner, where the broker MUST
    be resolvable. Required (not defaulted) so a wiring gap fails loudly
    instead of silently choosing the permissive branch.
    """
    raw = os.environ.get("RUNNER_IS_TRUSTED", "").strip().lower()
    if raw not in ("true", "false"):
        click.echo(
            f"ERROR: RUNNER_IS_TRUSTED must be 'true' or 'false', got {raw!r}. "
            "Refusing to guess whether a missing broker is an expected fork "
            "skip or a trusted-runner misconfiguration.",
            err=True,
        )
        sys.exit(1)
    return raw == "true"
