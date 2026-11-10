# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Probers and publishers of the C32 node: in-memory (the default) and the real local backend."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from omnimarket.nodes.node_c32_auth_boundary_probe_effect.handlers import probe_c32
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.handlers.probe_base import (
    ProbeResult,
)
from omnimarket.nodes.node_c32_auth_boundary_probe_effect.models.model_c32_probe import (
    ModelC32Clause,
    ModelC32Precondition,
    ModelC32ProbeReading,
)


class InMemoryC32Prober:
    """Grades nothing unless it is handed a reading: the default profile can never read green on its own."""

    def __init__(self, reading: ModelC32ProbeReading | None = None) -> None:
        self._reading = reading or ModelC32ProbeReading(
            preconditions=(
                ModelC32Precondition(
                    name="a real prober",
                    present=False,
                    detail="the inmemory profile starts no dashboard and grades no clause",
                    owner="OMN-23213",
                ),
            )
        )

    def run_probe(self) -> ModelC32ProbeReading:
        return self._reading


def reading_from_probe(result: ProbeResult) -> ModelC32ProbeReading:
    return ModelC32ProbeReading(
        preconditions=tuple(
            ModelC32Precondition(
                name=p.name, present=p.present, detail=p.detail, owner=p.owner
            )
            for p in result.preconditions
        ),
        clauses=tuple(
            ModelC32Clause(name=c.name, ok=c.ok, detail=c.detail)
            for c in result.clauses
        ),
    )


_VERSION_CODE = (
    "import json, sys\n"
    "from importlib.metadata import PackageNotFoundError, version\n"
    "out = {}\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        out[name] = version(name)\n"
    "    except PackageNotFoundError:\n"
    "        pass\n"
    "print(json.dumps(out))\n"
)


def installed_versions(
    bin_dir: str, packages: tuple[str, ...], timeout_s: int = 60
) -> dict[str, str]:
    """The version of each package installed in the environment whose ``bin`` is ``bin_dir``; an absent package or an
    unreadable environment gives fewer (or no) keys, which M1 reads as not the candidate."""
    import json

    python = Path(bin_dir) / "python"
    try:
        done = subprocess.run(
            [str(python), "-I", "-c", _VERSION_CODE, *packages],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        if done.returncode != 0:
            return {}
        raw = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


class LocalOnexC32Prober:
    """Runs the ported probe against the ``onex`` found first in ``onex_bin_dir``: the five cases against the dashboard
    process ``onex dashboard`` starts, and the planted-credential search, in a scratch HOME."""

    def __init__(
        self,
        onex_bin_dir: str,
        *,
        graded_packages: tuple[str, ...] = (),
        run: Callable[[], ProbeResult] = probe_c32.run,
        versions: Callable[[], dict[str, str]] | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self._bin_dir = onex_bin_dir
        self._versions = versions or (
            lambda: installed_versions(onex_bin_dir, graded_packages)
        )
        self._run = run
        self._environ = environ if environ is not None else os.environ

    def run_probe(self) -> ModelC32ProbeReading:
        before = self._environ.get("PATH", "")
        self._environ["PATH"] = (
            f"{self._bin_dir}{os.pathsep}{before}" if before else self._bin_dir
        )
        try:
            reading = reading_from_probe(self._run())
        finally:
            self._environ["PATH"] = before
        return reading.model_copy(update={"versions": self._versions()})


class InMemoryC32Publisher:
    """Records what would go on the bus; ``accept`` False models a bus that refuses."""

    def __init__(self, *, accept: bool = True) -> None:
        self.events: list[dict[str, object]] = []
        self._accept = accept

    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: Mapping[str, object],
        event_id: str,
        correlation_id: str | None = None,
        partition_key: str | None = None,
    ) -> bool:
        if self._accept:
            self.events.append(
                {
                    "topic": topic,
                    "event_type": event_type,
                    "payload": dict(payload),
                    "event_id": event_id,
                    "correlation_id": correlation_id,
                    "partition_key": partition_key,
                }
            )
        return self._accept
