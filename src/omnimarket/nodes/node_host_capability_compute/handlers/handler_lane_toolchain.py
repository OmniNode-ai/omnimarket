# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge a lab host's measured probes against its declared lane toolchain and name each missing or broken tool (OMN-22414).

Lanes tripped on a half-installed host one at a time (2026-11-03): the bare ``onex`` raised ``ModuleNotFoundError`` on one host,
pyright-python's nodeenv directory was empty on another, and two hosts had no node or pnpm. The toolchain each host needs is data
(``lane_toolchain.yaml``); this node holds no tool or host name. A tool the reading does not cover is not measured and never a
failure; a host the table does not declare is one.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.models.lane_toolchain import ModelLaneToolchainTable

from ..models.model_lane_toolchain import (
    ModelHostToolchainReading,
    ModelLaneToolchainFailure,
    ModelLaneToolchainRequest,
    ModelLaneToolchainResult,
    ModelToolReading,
)

TABLE_PATH = Path(__file__).resolve().parents[1] / "lane_toolchain.yaml"
# The exit status a probe reports for a command that was not found.
NOT_FOUND_STATUS = 127


def _missing(reading: ModelToolReading) -> bool:
    if reading.status == NOT_FOUND_STATUS:
        return True
    # A lab run once printed "command not found" and exited 0: the words count as much as the status.
    return reading.status == 0 and "command not found" in reading.tail.lower()


def _failure(
    table: ModelLaneToolchainTable,
    host: ModelHostToolchainReading,
    reading: ModelToolReading,
) -> ModelLaneToolchainFailure | None:
    if reading.status is None:
        return None
    spec = table.tools[reading.tool]
    if _missing(reading):
        state = "missing"
    elif reading.status != 0:
        state = "broken"
    else:
        return None
    probe = " ".join(spec.probe)
    said = f": {reading.tail}" if reading.tail else ""
    stale = (
        f"; {spec.repair.path} is an empty directory"
        if spec.repair.path and spec.repair.path in host.empty_dirs
        else ""
    )
    return ModelLaneToolchainFailure(
        host=host.host,
        tool=reading.tool,
        state=state,
        reason=f"`{probe}` {state} (exit {reading.status}){said}{stale}; {spec.owner_fix}",
    )


class HandlerLaneToolchain:
    def handle(self, request: ModelLaneToolchainRequest) -> ModelLaneToolchainResult:
        table = request.table
        found: list[ModelLaneToolchainFailure] = []
        for host in request.readings:
            declared = table.hosts.get(host.host)
            if declared is None:
                found.append(
                    ModelLaneToolchainFailure(
                        host=host.host,
                        tool="declaration",
                        state="undeclared",
                        reason="the lane toolchain table does not declare this host",
                    )
                )
                continue
            measured = {r.tool: r for r in host.tools}
            for tool in declared:
                reading = measured.get(tool)
                failure = _failure(table, host, reading) if reading else None
                if failure is not None:
                    found.append(failure)
        return ModelLaneToolchainResult(failures=tuple(found))
