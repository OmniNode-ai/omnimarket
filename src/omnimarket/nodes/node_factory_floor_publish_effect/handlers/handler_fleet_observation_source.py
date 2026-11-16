# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the canonical fleet producer's file, without adding another observer.

The producer and publisher run on different hosts. Read the file in the runtime
container that consumes fleet observations, using its installed contract and
state directory. A failed read replaces old local counts with an unread result.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.request
from collections.abc import Callable, Mapping
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
from omnimarket.models.model_runtime_host_reads import ModelRuntimeHostReadOverlay
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_delegation_report import (
    PublishRefusedError,
    Runner,
    run_process,
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


def fleet_document(data: Mapping[str, Any]) -> dict[str, Any]:
    """The document the floor reads, built through the floor's own typed model so a row it would refuse is refused here."""
    fleet = ModelFloorRunnerFleet(
        observed_at=data["observed_at"],
        runners=tuple(
            ModelFloorRunner(
                host_label=row["host"],
                status=row["status"],
                runner_name=row.get("runner_name"),
                label_class=row.get("label_class"),
                labels=tuple(row.get("labels") or ()),
            )
            for row in data["runners"]
        ),
    )
    document = {
        "status": "read",
        "observed_at": fleet.observed_at.isoformat(),
        "runners": [
            {
                "host": row.host_label,
                "status": row.status,
                "runner_name": row.runner_name,
                "label_class": row.label_class,
                "labels": list(row.labels),
            }
            for row in fleet.runners
        ],
    }
    return document


Fetch = Callable[[str, int], bytes]


def fetch_url(url: str, timeout: int) -> bytes:
    """The one HTTP boundary of this node; tests replace it. Raises ``OSError`` on a connection, status or timeout failure."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return bytes(response.read())


def fleet_document_from_projection(body: object) -> dict[str, Any]:
    """The raw fleet observation (the saved file's shape), from the runner fleet projection's served page.

    A page that is partial, empty or without an event time is refused: a cut page would read as a smaller, healthy
    fleet, which is the false green the whole fleet observation exists to prevent.
    """
    if not isinstance(body, dict):
        raise ValueError("fleet projection output is not an object")
    rows = body.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("fleet projection holds no runner rows")
    if body.get("truncated") or body.get("next_cursor"):
        raise ValueError("fleet projection page is partial")
    if body.get("row_count") != len(rows):
        raise ValueError("fleet projection row count does not match its rows")
    observed_at = body.get("latest_event_at")
    if not observed_at:
        raise ValueError("fleet projection carries no latest_event_at")
    return {"observed_at": observed_at, "runners": rows}


class HandlerFleetObservationSource:
    """The publisher's read seam for the bus producer's saved observation."""

    def __init__(
        self, runner: Runner | None = None, fetch: Fetch | None = None
    ) -> None:
        self._run = run_process if runner is None else runner
        self._fetch = fetch_url if fetch is None else fetch

    def _read_ssh(
        self,
        overlay: ModelRuntimeHostReadOverlay,
        env: Mapping[str, str],
        timeout: int,
    ) -> dict[str, Any]:
        host = env.get(overlay.ssh_host_env, "").strip()
        if not host:
            raise ValueError(f"{overlay.ssh_host_env} is unset")
        script = "\n".join(
            [
                "set -eu",
                "RT=$(docker ps --filter "
                f"label=com.docker.compose.project={overlay.compose_project} --filter "
                f"label=com.docker.compose.service={overlay.fleet_service} "
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
            timeout,
        )
        if result.returncode:
            raise ValueError(f"fleet producer read exited {result.returncode}")
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError("fleet producer output is not an object")
        if data.get("status") == "unread":
            raise ValueError(str(data.get("reason") or "fleet producer unread"))
        return data

    def _read_raw(
        self,
        overlay: ModelRuntimeHostReadOverlay,
        env: Mapping[str, str],
        bound: int,
    ) -> dict[str, Any]:
        """The raw observation: the bus projection first when the overlay names it, ssh only when that fails.

        Both reads together stay inside ``bound`` (the step's own timeout), which this never raises.
        """
        if overlay.fleet_projection_url is None:
            return self._read_ssh(overlay, env, bound)
        started = time.monotonic()
        try:
            raw = fleet_document_from_projection(
                json.loads(
                    self._fetch(
                        overlay.fleet_projection_url,
                        min(overlay.fleet_projection_timeout_seconds, bound),
                    )
                )
            )
            fleet_document(
                raw
            )  # a page the floor's model refuses falls back like a cut page
            return raw
        except (OSError, ValueError, KeyError, TypeError) as projection_exc:
            remaining = bound - math.ceil(time.monotonic() - started)
            if remaining < 1:
                raise ValueError(
                    f"fleet projection read failed ({projection_exc}); no time left in the {bound} s bound for ssh"
                ) from projection_exc
            try:
                return self._read_ssh(overlay, env, remaining)
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                PublishRefusedError,
            ) as ssh_exc:
                raise ValueError(
                    f"fleet projection read failed ({projection_exc}); ssh read failed ({ssh_exc})"
                ) from ssh_exc

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
                "runtime_host_reads",
                ModelRuntimeHostReadOverlay,
                env,
                file_env="RUNTIME_HOST_READS_OVERLAY_PATH",
            )
            data = self._read_raw(overlay, env, request.timeout_seconds)
            document = fleet_document(data)
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
