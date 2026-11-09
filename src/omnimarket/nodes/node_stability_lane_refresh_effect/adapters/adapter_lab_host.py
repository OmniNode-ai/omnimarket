# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-host profile of node_stability_lane_refresh_effect: files, git, a subprocess and an HTTP read."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path

from omnimarket.nodes.node_stability_lane_refresh_effect.models import (
    ModelLaneHealthReading,
    ModelReleasePinSet,
    ModelRunOutcome,
)

_APPLIED_FILE = "applied-pin.json"
_TAIL_CHARS = 1200


class FilePinSource:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def read_text(self) -> str:
        return self._path.read_text(encoding="utf-8")


class FileAppliedPinStore:
    """The applied set, one JSON file in the overlay's state directory."""

    def __init__(self, state_dir: str) -> None:
        self._dir = Path(state_dir)

    def read(self) -> ModelReleasePinSet | None:
        path = self._dir / _APPLIED_FILE
        if not path.is_file():
            return None
        return ModelReleasePinSet.model_validate_json(path.read_text(encoding="utf-8"))

    def write(self, applied: ModelReleasePinSet) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / _APPLIED_FILE).write_text(
            applied.model_dump_json(), encoding="utf-8"
        )

    def render_pin_file(self, text: str) -> str:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / "bump-pin.yaml"
        path.write_text(text, encoding="utf-8")
        return str(path)


class SubprocessRefreshRunner:
    """Runs the refresh script with the overlay's environment laid over this process's."""

    def run(
        self, argv: Sequence[str], env: Mapping[str, str], timeout_seconds: int
    ) -> ModelRunOutcome:
        try:
            done = subprocess.run(
                list(argv),
                env={**os.environ, **env},
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ModelRunOutcome(
                return_code=124, output_tail=f"timed out after {timeout_seconds}s"
            )
        tail = (done.stdout + done.stderr)[-_TAIL_CHARS:]
        return ModelRunOutcome(return_code=done.returncode, output_tail=tail)


class GitTagCheck:
    def __init__(self, registry_root: str, timeout_seconds: int) -> None:
        self._home = Path(registry_root)
        self._timeout = timeout_seconds

    def has_tag(self, repo: str, tag: str) -> bool:
        done = subprocess.run(
            [
                "git",
                "-C",
                str(self._home / repo),
                "rev-parse",
                "-q",
                "--verify",
                f"refs/tags/{tag}^{{commit}}",
            ],
            capture_output=True,
            text=True,
            timeout=self._timeout,
            check=False,
        )
        return done.returncode == 0


class HttpLaneHealth:
    """The runtime's /health: version, the event bus environment and the runtime wiring component."""

    def __init__(self, url: str, timeout_seconds: int) -> None:
        self._url = url
        self._timeout = timeout_seconds

    def read(self) -> ModelLaneHealthReading:
        with urllib.request.urlopen(self._url, timeout=self._timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        details = body.get("details", {})
        components = details.get("components", {})
        wiring = components.get("runtime_wiring", {}).get("status")
        return ModelLaneHealthReading(
            version=str(body.get("version", "")),
            environment=str(details.get("event_bus", {}).get("environment", "")),
            wiring_healthy=body.get("status") == "healthy" and wiring == "healthy",
        )


__all__ = [
    "FileAppliedPinStore",
    "FilePinSource",
    "GitTagCheck",
    "HttpLaneHealth",
    "SubprocessRefreshRunner",
]
