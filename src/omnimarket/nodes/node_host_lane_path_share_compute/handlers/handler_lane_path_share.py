# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Alarm, with a named owner, while a host's mounted data drives hold under 10 percent of its lane paths (OMN-25931).

The switchover of lane paths onto the data drives was ordered, owned twice and dropped twice because nothing
measured it. This reads the lane path sizes a host reports (worktrees, run dirs, tmp, caches, Codex home, runner
work dirs), sums the bytes that sit on a mounted data drive and compares that share to the bar. Under the bar the
host gets one alarm row naming its owner; at or over it, one ok row, which resolves an open alarm. A host that
reports no bytes is indeterminate, never ok: a zero is not a pass. The rows are ledger STATUS rows
(``alarm=lane-path-share``) that the failure alarm router reads and delivers to the live orchestrator inbox and the
operator push channel; this node sends nothing itself.
"""

from __future__ import annotations

from datetime import UTC

from omnimarket.nodes.node_host_lane_path_share_compute.models.model_lane_path_share import (
    ModelHostLanePaths,
    ModelHostShare,
    ModelLanePathShareRequest,
    ModelLanePathShareResult,
)

ACTOR = "node:node_host_lane_path_share_compute"
ROW_LANE = "host-lane-path-share"
ALARM = "lane-path-share"
OK = "lane-path-share-ok"


def _cell(value: object) -> str:
    return " ".join(str(value).replace("|", "/").split())


def _judge(host: ModelHostLanePaths, bar_percent: float) -> ModelHostShare:
    total = sum(p.bytes_used for p in host.paths)
    drives = set(host.data_drive_mounts)
    on_drive = sum(p.bytes_used for p in host.paths if p.mount_point in drives)
    if total == 0:
        return ModelHostShare(
            host_name=host.host_name,
            owner=host.owner,
            data_drive_bytes=0,
            total_bytes=0,
            share_percent=0.0,
            state="indeterminate",
            reason="the host reported no lane path bytes, so no share can be read",
        )
    share = 100.0 * on_drive / total
    return ModelHostShare(
        host_name=host.host_name,
        owner=host.owner,
        data_drive_bytes=on_drive,
        total_bytes=total,
        share_percent=round(share, 2),
        state="alarm" if share < bar_percent else "ok",
    )


class HandlerLanePathShare:
    @property
    def handler_type(self) -> str:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> str:
        return "COMPUTE"

    def handle(self, request: ModelLanePathShareRequest) -> ModelLanePathShareResult:
        at = request.now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        hosts = tuple(_judge(h, request.bar_percent) for h in request.hosts)
        by_name = {h.host_name: h for h in request.hosts}
        rows: list[str] = []
        for h in hosts:
            if h.state == "indeterminate":
                continue
            declared = by_name[h.host_name]
            alarm = OK if h.state == "ok" else ALARM
            drives = ",".join(sorted(declared.data_drive_mounts)) or "none"
            verb = "under" if h.state == "alarm" else "at or over"
            rows.append(
                f"{at} | STATUS | lane={ROW_LANE} | actor={ACTOR} | model=none | src=alarm | alarm={alarm}"
                f" | host={_cell(h.host_name)} | share_pct={h.share_percent:g} | bar_pct={request.bar_percent:g}"
                f" | data_drives={_cell(drives)} | owner={_cell(h.owner)}"
                f" | {_cell(h.host_name)}: {h.share_percent:g} percent of lane path bytes sit on a mounted data drive,"
                f" {verb} the {request.bar_percent:g} percent bar"
            )
        refused = [h for h in hosts if h.state == "indeterminate"]
        if refused:
            names = ",".join(h.host_name for h in refused)
            return ModelLanePathShareResult(
                status="refused",
                exit_code=65,
                reason=f"no lane path bytes reported for: {names}",
                hosts=hosts,
                rows=tuple(rows),
            )
        return ModelLanePathShareResult(status="success", hosts=hosts, rows=tuple(rows))
