# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lanes start containers (a test database) that no closeout removes (OMN-21288).

The lane reaper never touches docker. Lanes are expected to label what they start
with onex.lane=<lane>.
"""

from ..models.model_lane_stack_orphan import (
    ModelLaneStackFinding,
    ModelLaneStackOrphanRequest,
    ModelLaneStackOrphanVerdict,
)

PROTECTED_PREFIXES = (
    "omnibase-infra",
    "omninode-",
    "omnimarket-",
    "onex-api",
    "dockerhub-mirror",
    "omnibase-intelligence",
)


class HandlerLaneStackOrphan:
    def handle(
        self, request: ModelLaneStackOrphanRequest
    ) -> ModelLaneStackOrphanVerdict:
        findings: list[ModelLaneStackFinding] = []
        stop_names: list[str] = []
        unowned_names: list[str] = []
        mem_bytes_freed = 0
        for container in request.containers:
            if container.name.startswith(PROTECTED_PREFIXES):
                finding = ModelLaneStackFinding(
                    name=container.name,
                    verdict="keep_protected",
                    reason="Protected container; a lane label cannot claim it.",
                )
            elif container.lane == "":
                finding = ModelLaneStackFinding(
                    name=container.name,
                    verdict="unowned",
                    reason="No onex.lane label, so the owner is unknown.",
                )
                unowned_names.append(container.name)
            elif container.lane in request.live_lanes:
                finding = ModelLaneStackFinding(
                    name=container.name,
                    verdict="keep_live_lane",
                    reason=f"Lane {container.lane} is live.",
                )
            elif container.lane in request.finished_lanes:
                if container.state != "running":
                    finding = ModelLaneStackFinding(
                        name=container.name,
                        verdict="already_stopped",
                        reason=f"Lane {container.lane} is finished and the container is not running.",
                    )
                else:
                    finding = ModelLaneStackFinding(
                        name=container.name,
                        verdict="stop",
                        reason=f"Lane {container.lane} is finished and left the container running.",
                        stop_command=f"docker stop {container.name}",
                        restore_command=f"docker start {container.name}",
                    )
                    stop_names.append(container.name)
                    mem_bytes_freed += container.mem_bytes
            else:
                finding = ModelLaneStackFinding(
                    name=container.name,
                    verdict="keep_unknown_lane",
                    reason=f"Lane {container.lane} is neither live nor finished.",
                )
            findings.append(finding)
        return ModelLaneStackOrphanVerdict(
            findings=tuple(findings),
            stop_names=tuple(stop_names),
            unowned_names=tuple(unowned_names),
            mem_bytes_freed=mem_bytes_freed,
        )
