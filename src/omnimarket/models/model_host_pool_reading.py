# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One declared host's pool read, shared by the host pool alarm and the remote-lane placement (OMN-17232).

The model lives in ``omnimarket.models``, not in ``node_host_pool_alarm_effect``, so
``node_remote_lane_compute`` builds it without a cross-node reach-in (see
``tests/test_no_cross_node_reach_in.py``). It is a frozen dataclass, as it was in the alarm node: the alarm's
callers on installed plugin copies have only the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelHostPoolReading:
    """One declared host's pool read, from the probe's own fields (landing_placement's ``Reading``)."""

    host: str
    lane_cap: int
    error: (
        str | None
    )  # why the read failed (UNREADABLE, a timeout); None: the host answered
    available: bool = True  # False: the shared table marks the host down, which is its own state and not this alarm's
    load1: float | None = None
    swap_used_frac: float | None = None
    swap_in_kb_s: float | None = None
    swap_out_kb_s: float | None = None
    mem_psi_avg60: float | None = None
    mem_pressure: int | None = None
