# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the canonical fleet producer's file, without adding another observer.

The producer and publisher run on different hosts. Read the file in the runtime
container that consumes fleet observations, using its installed contract and
state directory. A failed read replaces old local counts with an unread result.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.model_factory_floor_input import (
    ModelFloorRunner,
    ModelFloorRunnerFleet,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_delegation_report import (
    PublishRefusedError,
    Runner,
    run_process,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_delegation_report import (
    ModelDelegationReadOverlay,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_fleet_observation_source import (
    ModelFleetObservationSourceRequest,
    ModelFleetObservationSourceResult,
)

READ_FLEET_PY = """
# @@FLEET: read the file owned by the existing bus projection.
import json, os, sys
from importlib.resources import files
from pathlib import Path
import yaml
try:
 contract = yaml.safe_load(files('omnimarket.nodes.node_projection_runner_fleet').joinpath('contract.yaml').read_text())
 declared = contract['state_file']
 directory = os.environ.get(declared['dir_env'], '').strip()
 if not directory:
  raise ValueError(declared['dir_env'] + ' is unset in the fleet consumer')
 sys.stdout.write((Path(directory) / declared['relpath']).read_text())
except (OSError, KeyError, ValueError) as exc:
 print(json.dumps({'status': 'unread', 'reason': 'fleet producer: ' + str(exc)}))
"""


class HandlerFleetObservationSource:
    """The publisher's read seam for the bus producer's saved observation."""

    def __init__(self, runner: Runner | None = None) -> None:
        self._run = run_process if runner is None else runner

    def handle(
        self,
        request: ModelFleetObservationSourceRequest,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> ModelFleetObservationSourceResult:
        env = os.environ if environ is None else environ
        document: dict[str, Any]
        try:
            overlay = request.overlay or load_structured_overlay(
                "delegation_health",
                ModelDelegationReadOverlay,
                env,
                file_env="DELEGATION_HEALTH_OVERLAY_PATH",
            )
            host = env.get(overlay.ssh_host_env, "").strip()
            if not host:
                raise ValueError(f"{overlay.ssh_host_env} is unset")
            script = "\n".join(
                [
                    "set -eu",
                    "RT=$(docker ps --filter "
                    f"label=com.docker.compose.project={overlay.compose_project} --filter "
                    f"label=com.docker.compose.service={overlay.runtime_service} "
                    "--filter status=running --format '{{.Names}}' | head -1)",
                    '[ -n "$RT" ]',
                    "docker exec -i \"$RT\" python3 - <<'PY'",
                    READ_FLEET_PY,
                    "PY",
                ]
            )
            result = self._run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    "--",
                    host,
                    "bash -s",
                ],
                script.encode(),
                request.timeout_seconds,
            )
            if result.returncode:
                raise ValueError(f"fleet producer read exited {result.returncode}")
            data = json.loads(result.stdout)
            if not isinstance(data, dict):
                raise ValueError("fleet producer output is not an object")
            if data.get("status") == "unread":
                raise ValueError(str(data.get("reason") or "fleet producer unread"))
            fleet = ModelFloorRunnerFleet(
                observed_at=data["observed_at"],
                runners=tuple(
                    ModelFloorRunner(host_label=row["host"], status=row["status"])
                    for row in data["runners"]
                ),
            )
            document = {
                "status": "read",
                "observed_at": fleet.observed_at.isoformat(),
                "runners": [
                    {"host": row.host_label, "status": row.status}
                    for row in fleet.runners
                ],
            }
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            NodeOverlayError,
            PublishRefusedError,
        ) as exc:
            document = {
                "status": "unread",
                "reason": str(exc),
                "generated_at": datetime.now(UTC).isoformat(),
            }
        return ModelFleetObservationSourceResult(document=document)
