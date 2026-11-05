# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Backends for the conformance suite.

``in_memory`` always runs. ``temporal_self_hosted`` runs when ``RUN_ENGINE_TEMPORAL_OVERLAY`` names an overlay file
that selects it: the suite starts its own worker process against the server that overlay names, and every run key is
unique, so a shared server is safe. No server address is packaged in this repository.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_run_engine_effect.backends.protocol_run_engine_backend import (
    ProtocolRunEngineBackend,
)
from omnimarket.nodes.node_run_engine_effect.handlers.handler_run_engine import (
    HandlerRunEngine,
)
from omnimarket.nodes.node_run_engine_effect.handlers.overlay_run_engine import (
    NODE_OVERLAY_ID,
    build_backend,
    load_run_engine_overlay,
)
from omnimarket.nodes.node_run_engine_effect.models.model_run_engine import (
    ModelRunEngineOverlay,
)

TEMPORAL_OVERLAY_ENV = "RUN_ENGINE_TEMPORAL_OVERLAY"
HELPER = Path(__file__).parent / "helper_keyed_effect.py"


@dataclass
class Harness:
    """What a scenario needs: the node handler, the ledger file, and process control for the durable backend."""

    kind: str
    backend: ProtocolRunEngineBackend
    ledger: Path
    overlay_root: Path | None
    workers: list[subprocess.Popen[bytes]] = field(default_factory=list)

    @property
    def handler(self) -> HandlerRunEngine:
        return HandlerRunEngine(self.backend)

    def key(self, label: str) -> str:
        return f"conf-{label}-{uuid.uuid4().hex[:10]}"

    def command(
        self, *, idem: int = 1, after: float = 0.0, exit_code: int = 0
    ) -> tuple[str, ...]:
        return (
            sys.executable,
            str(HELPER),
            "--ledger",
            str(self.ledger),
            "--idem",
            str(idem),
            "--after",
            str(after),
            "--exit",
            str(exit_code),
        )

    def rows(self, run_key: str, step: str) -> int:
        if not self.ledger.exists():
            return 0
        import json

        key = f"{run_key}/{step}"
        return sum(
            1
            for line in self.ledger.read_text().splitlines()
            if json.loads(line)["key"] == key
        )

    def start_worker(self) -> None:
        if self.overlay_root is None:
            return
        env = {**os.environ, "ONEX_SKILL_OVERLAY_ROOTS": str(self.overlay_root)}
        self.workers.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "omnimarket.nodes.node_run_engine_effect.backends.worker_temporal",
                ],
                env=env,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        time.sleep(2.0)

    def kill_workers(self) -> None:
        for proc in self.workers:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        self.workers.clear()


def _temporal_overlay_path() -> Path | None:
    raw = os.environ.get(TEMPORAL_OVERLAY_ENV, "").strip()
    return Path(raw) if raw else None


BACKENDS = ["in_memory"] + (
    ["temporal_self_hosted"] if _temporal_overlay_path() else []
)


def _build(kind: str, tmp_path: Path) -> Harness:
    ledger = tmp_path / "ledger.jsonl"
    if kind == "in_memory":
        overlay = load_run_engine_overlay(environ={}, explicit=None)
        return Harness(kind, build_backend(overlay), ledger, None)
    source = _temporal_overlay_path()
    assert source is not None
    root = tmp_path / "overlays"
    (root / NODE_OVERLAY_ID).mkdir(parents=True)
    (root / NODE_OVERLAY_ID / "overlay.yaml").write_text(source.read_text())
    overlay = ModelRunEngineOverlay.model_validate(yaml.safe_load(source.read_text()))
    h = Harness(kind, build_backend(overlay), ledger, root)
    h.start_worker()
    return h


@pytest.fixture(params=BACKENDS)
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Harness]:
    h = _build(request.param, tmp_path)
    try:
        yield h
    finally:
        h.kill_workers()


@pytest.fixture
def durable(tmp_path: Path) -> Iterator[Harness]:
    """The durable backend only; used by the tests that exist only when the overlay selects it."""
    h = _build("temporal_self_hosted", tmp_path)
    try:
        yield h
    finally:
        h.kill_workers()
