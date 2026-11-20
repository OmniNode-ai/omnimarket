# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reader of a lab host's declared bus lane (OMN-24577).

A lab host carries no KAFKA_BOOTSTRAP_SERVERS, but its lab host table row names
the bus lane it publishes on. ``onex secret``'s tenant-key registration and
``onex models review-gateway`` both read the lane through this module and open
that lane's bus with the lab-run lane resolver; the table itself is the
deployment fact named by ``ONEX_LAB_RUN_HOSTS``.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def lab_host_bus_lane(table: Path | None, host: str | None) -> str | None:
    """The host's declared ``ledger_bus_lane`` in the lab host table (OMN-20910).

    A lab host carries no KAFKA_BOOTSTRAP_SERVERS, but its lab host table row
    names the bus lane it publishes on; the lane's broker and login come from
    the lane declaration, never from the shell. No table, no host, or a table
    that cannot be read reads as no lane.
    """
    if table is None or host is None:
        return None
    try:
        document = yaml.safe_load(table.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    rows = document.get("hosts") if isinstance(document, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("name") == host:
            lane = row.get("ledger_bus_lane")
            return lane.strip() if isinstance(lane, str) and lane.strip() else None
    return None
