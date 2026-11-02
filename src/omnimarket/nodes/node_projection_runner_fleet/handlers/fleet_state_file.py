# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The newest fleet observation, saved as one JSON file the factory floor reads (OMN-21733).

The floor's machine bays draw each machine's CI runners from ``runner-fleet.json`` under the state directory. The runner
monitor publishes the observation to the bus and this node is its consumer, so this node saves it: the file holds the
observation's time and, per runner, the host label it carries and its status (the two fields the floor reads).

The file is replaced only by an observation newer than the one it holds, so a redelivered older observation never
overwrites a newer one, and it is written to a temporary name and renamed into place so a reader never sees half of it.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_projection_runner_fleet.models import (
    ModelRunnerFleetObservationWire,
)

SAVED = "saved"
SUPERSEDED = "superseded"


def state_file_path(
    contract: Mapping[str, Any], environ: Mapping[str, str]
) -> Path | None:
    """Where the contract's ``state_file`` lands under the state directory its ``dir_env`` names; None when that is unset."""
    declared = contract["state_file"]
    directory = environ.get(str(declared["dir_env"]), "").strip()
    return Path(directory) / str(declared["relpath"]) if directory else None


def _held(path: Path) -> datetime | None:
    try:
        return datetime.fromisoformat(
            str(json.loads(path.read_text(encoding="utf-8"))["observed_at"])
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_observation(observation: ModelRunnerFleetObservationWire, path: Path) -> str:
    """Write the observation to ``path`` unless the file already holds a newer or equal one; returns ``saved`` or ``superseded``."""
    held = _held(path)
    if held is not None and held >= observation.observed_at:
        return SUPERSEDED
    document = {
        "observed_at": observation.observed_at.isoformat(),
        "observing_host": observation.host,
        "runners": [
            {"host": runner.host, "status": runner.status.value}
            for runner in observation.runners
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return SAVED
