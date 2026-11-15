# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The other side of every topic the host install orchestrator speaks to, for its tests (OMN-23957).

``Plant`` answers each command the orchestrator publishes: a stand-in for the registry installer, the real file effect
over an in-memory file store, the real readback compute, and the real reducer fold. Nothing here decides for the
orchestrator: it only routes what was published to the node that owns the topic and hands back what that node answered.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import yaml

from omnimarket.nodes.node_host_install_file_effect.handlers.handler_host_install_file import (
    HandlerHostInstallFile,
)
from omnimarket.nodes.node_host_install_file_effect.models.model_host_install_file import (
    ModelHostInstallFileRequest,
)
from omnimarket.nodes.node_host_install_file_effect.overlay import (
    ModelHostInstallFileOverlay,
)
from omnimarket.nodes.node_host_install_orchestrator.handlers.handler_host_install_orchestrator import (
    TOPIC_COMPLETED,
    TOPIC_FILE_REQUESTED,
    TOPIC_READBACK_REQUESTED,
    TOPIC_REGISTRY_REQUESTED,
    TOPIC_STEP,
    HandlerHostInstallOrchestrator,
)
from omnimarket.nodes.node_host_install_orchestrator.models import (
    ModelHostInstallCompleted,
    ModelHostInstallRequest,
)
from omnimarket.nodes.node_host_install_orchestrator.overlay import (
    ModelHostInstallOrchestratorOverlay,
)
from omnimarket.nodes.node_host_install_readback_compute.handlers.handler_host_install_readback import (
    HandlerHostInstallReadback,
)
from omnimarket.nodes.node_host_install_readback_compute.models.model_host_install_readback import (
    ModelHostInstallReadbackRequest,
)
from omnimarket.nodes.node_host_install_readback_compute.overlay import (
    ModelHostInstallReadbackOverlay,
)
from omnimarket.nodes.node_host_install_state_reducer.handlers.handler_host_install_state import (
    HostInstallTransitionError,
    fold,
)
from omnimarket.nodes.node_host_install_state_reducer.models import (
    ModelHostInstallRecord,
    ModelHostInstallReduceCommand,
)
from omnimarket.nodes.node_host_install_state_reducer.overlay import (
    ModelHostInstallStateOverlay,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
    ModelRegistryUnitsResult,
)
from tests.nodes.node_host_install_file_effect.support import FakeHostInstallFiles

NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
NODE_DIR = NODES / "node_host_install_orchestrator"
START = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
HOME = "/home/example"
LABEL = "example-tick"
SERVICE_PATH = f"{HOME}/.config/systemd/user/{LABEL}.service"
TIMER_PATH = f"{HOME}/.config/systemd/user/{LABEL}.timer"
NEW_SERVICE = "[Service]\nExecStart=/usr/bin/example new\n"
NEW_TIMER = "[Timer]\nOnActiveSec=1s\n"
OLD_SERVICE = "[Service]\nExecStart=/usr/bin/example old\n"
OLD_TIMER = "[Timer]\nOnActiveSec=9s\n"


def _document(node: str) -> dict[str, object]:
    loaded = yaml.safe_load((NODES / node / "overlay.example.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def orchestrator_overlay() -> ModelHostInstallOrchestratorOverlay:
    return ModelHostInstallOrchestratorOverlay.model_validate(
        _document("node_host_install_orchestrator")
    )


def request(correlation_id: UUID | None = None) -> ModelHostInstallRequest:
    return ModelHostInstallRequest(
        correlation_id=correlation_id or uuid4(),
        host="host-a",
        unit="unit-a",
        actor="lane-test",
        platform="systemd",
        label=LABEL,
        registry_request=ModelRegistryUnitsRequest(
            unit="unit-a", home=HOME, registry_root="/srv/registry"
        ),
    )


class StepClock:
    """A clock that moves one second per read, so every step event has its own time."""

    def __init__(self) -> None:
        self.ticks = 0

    def __call__(self) -> datetime:
        self.ticks += 1
        return START + timedelta(seconds=self.ticks)


class Plant:
    """Routes every published payload to the node that owns its topic and queues the answer."""

    def __init__(
        self,
        orchestrator: HandlerHostInstallOrchestrator,
        files: FakeHostInstallFiles,
        *,
        render_ok: bool = True,
        install_ok: bool = True,
        install_timer: str = NEW_TIMER,
        run_ok: bool = True,
        drop_manifest_on_restore: bool = False,
    ) -> None:
        self.orchestrator = orchestrator
        self.files = files
        self.render_ok = render_ok
        self.install_ok = install_ok
        self.install_timer = install_timer
        self.run_ok = run_ok
        self.drop_manifest_on_restore = drop_manifest_on_restore
        self.file_effect = HandlerHostInstallFile(
            overlay=ModelHostInstallFileOverlay.model_validate(
                _document("node_host_install_file_effect")
            ),
            files=files,
        )
        self.readback = HandlerHostInstallReadback(
            overlay=ModelHostInstallReadbackOverlay.model_validate(
                _document("node_host_install_readback_compute")
            )
        )
        self.reducer_overlay = ModelHostInstallStateOverlay.model_validate(
            _document("node_host_install_state_reducer")
        )
        self.topics: list[str] = []
        self.records: list[ModelHostInstallRecord] = []
        self.refusals: list[str] = []
        self.steps: list[ModelHostInstallReduceCommand] = []
        self.completed: ModelHostInstallCompleted | None = None
        self.install_id: UUID | None = None

    def run(self, start: ModelHostInstallRequest) -> None:
        self.install_id = start.correlation_id
        queue: deque[tuple[object, UUID]] = deque([(start, start.correlation_id)])
        while queue:
            payload, key = queue.popleft()
            for out in self.orchestrator.decide(payload, key):
                self.topics.append(out.topic)
                answer = self._serve(out.topic, out.payload)
                if answer is not None:
                    queue.append((answer, key))

    def _serve(self, topic: str, payload: object) -> object | None:
        if topic == TOPIC_COMPLETED:
            assert isinstance(payload, ModelHostInstallCompleted)
            self.completed = payload
            return None
        if topic == TOPIC_STEP:
            assert isinstance(payload, ModelHostInstallReduceCommand)
            self.steps.append(payload)
            try:
                record = fold(payload.prior, payload.event, self.reducer_overlay)
            except HostInstallTransitionError as refusal:
                self.refusals.append(str(refusal))
                return None
            self.records.append(record)
            return record
        if topic == TOPIC_FILE_REQUESTED:
            assert isinstance(payload, ModelHostInstallFileRequest)
            if payload.mode == "restore" and self.drop_manifest_on_restore:
                for path in list(self.files._files):
                    if path.endswith("manifest.json"):
                        self.files.remove(path)
            return self.file_effect.handle(payload)
        if topic == TOPIC_READBACK_REQUESTED:
            assert isinstance(payload, ModelHostInstallReadbackRequest)
            return self.readback.handle(payload)
        if topic == TOPIC_REGISTRY_REQUESTED:
            assert isinstance(payload, ModelRegistryUnitsRequest)
            return self._registry(payload)
        raise AssertionError(f"the orchestrator published on an unknown topic {topic}")

    def _registry(self, command: ModelRegistryUnitsRequest) -> ModelRegistryUnitsResult:
        def result(
            status: str, *, rendered: str = "", stdout: str = "", stderr: str = ""
        ) -> ModelRegistryUnitsResult:
            return ModelRegistryUnitsResult(
                unit=command.unit,
                mode=command.mode,
                status=status,
                exit_code=0 if status == "success" else 1,
                rendered=rendered,
                stdout=stdout,
                stderr=stderr,
            )

        if command.mode == "dry-run":
            if not self.render_ok:
                return result("failure", stderr="uv is not on PATH")
            return result("success", rendered=NEW_SERVICE + "\n" + NEW_TIMER)
        if command.mode == "install":
            if not self.install_ok:
                return result("failure", stderr="unit refused")
            self.files.seed(SERVICE_PATH, NEW_SERVICE)
            self.files.seed(TIMER_PATH, self.install_timer)
            return result("success")
        if command.mode == "status":
            return result("success" if self.run_ok else "failure", stdout="active")
        raise AssertionError(f"the orchestrator sent mode {command.mode}")
