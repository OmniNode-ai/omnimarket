# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Test support for the host introspection nodes (OMN-19998): builders for readings, requests and a fake machine.

Hosts are RFC 5737 / ``.test`` style names; no value here is a deployment fact. The overlay is the example the effect node
ships, loaded through the overlay reader like production.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.host_introspection_overlay import (
    load_host_introspection_overlay,
)
from omnimarket.models.model_host_introspection_announcement import (
    EnumHostAnnouncementReason,
    EnumSoftwareKind,
    ModelHostFacts,
    ModelHostGpu,
    ModelHostIdentity,
    ModelHostModelServed,
    ModelHostRuntime,
    ModelHostSoftware,
)
from omnimarket.models.model_host_introspection_request import (
    ModelHostIntrospectionOverlay,
    ModelHostIntrospectionRequest,
    ModelHostRowFacts,
    ModelLaneAccountDeclaration,
)
from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols.local_host_reader import (
    HostReading,
)

NOW = datetime(2026, 11, 14, 4, 0, 0, tzinfo=UTC)
HOST = "host-a.test"
EXAMPLE_OVERLAY = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_host_introspection_announce_effect/overlay.example.yaml"
)
CAPACITY_EXAMPLE_OVERLAY = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_host_capacity_compute/overlay.example.yaml"
)
GIB = 1024**3


def install_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> ModelHostIntrospectionOverlay:
    """The shipped examples as the ``host_introspection`` and ``host_capacity`` overlays, loaded the way production loads them."""
    root = tmp_path / "overlay_roots"
    for overlay_id, example in (
        ("host_introspection", EXAMPLE_OVERLAY),
        ("host_capacity", CAPACITY_EXAMPLE_OVERLAY),
    ):
        target = root / overlay_id / "overlay.yaml"
        target.parent.mkdir(parents=True)
        target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    return load_host_introspection_overlay()


def capacity(
    *,
    host: str = HOST,
    tools: Sequence[str] = ("git", "claude-login"),
    load1: float = 1.5,
    mem_available_bytes: int = 40 * GIB,
    refusal: str = "",
) -> ModelHostCapacityAdvertisement:
    return ModelHostCapacityAdvertisement(
        host_name=host,
        cores=32,
        load1=load1,
        mem_available_bytes=mem_available_bytes,
        tools=list(tools),
        running_units=0,
        max_units=1,
        advertised_at=NOW,
        cadence_seconds=30,
        mem_total_bytes=64 * GIB,
        capacity_refusal=refusal,
    )


def facts(
    *,
    python_version: str = "3.12.3",
    gpu_used: int = 0,
    commit: str = "a" * 40,
) -> ModelHostFacts:
    return ModelHostFacts(
        identity=ModelHostIdentity(
            hostnames=(HOST,),
            os="Linux",
            arch="x86_64",
            kernel="6.8.0",
            boot_id="boot-1",
        ),
        gpus=(
            ModelHostGpu(
                name="gpu-b", mem_total_bytes=24 * GIB, mem_used_bytes=gpu_used
            ),
            ModelHostGpu(
                name="gpu-a", mem_total_bytes=24 * GIB, mem_used_bytes=gpu_used
            ),
        ),
        models_served=(
            ModelHostModelServed(
                name="model-x", port=8000, endpoint="http://host-a.test:8000"
            ),
        ),
        runtimes=(
            ModelHostRuntime(name="uv", version="0.9.0"),
            ModelHostRuntime(name="python", version=python_version),
        ),
        software=(
            ModelHostSoftware(
                name="omnimarket", kind=EnumSoftwareKind.PACKAGE, version="1.2.3"
            ),
            ModelHostSoftware(
                name="omnimarket", kind=EnumSoftwareKind.CLONE, commit=commit
            ),
        ),
    )


def request(
    *,
    reason: EnumHostAnnouncementReason = EnumHostAnnouncementReason.START,
    row: ModelHostRowFacts | None = None,
    cap: ModelHostCapacityAdvertisement | None = None,
    host_facts: ModelHostFacts | None = None,
    accounts: tuple[ModelLaneAccountDeclaration, ...] | None = None,
) -> ModelHostIntrospectionRequest:
    return ModelHostIntrospectionRequest(
        host_name=HOST,
        reason=reason,
        announced_at=NOW,
        announcement_id=uuid.UUID(int=7),
        cadence_seconds=30,
        facts=host_facts or facts(),
        capacity=cap or capacity(),
        row=row or ModelHostRowFacts(declared=True, roles=("lab", "lab-proof")),
        lane_accounts=accounts
        if accounts is not None
        else (
            ModelLaneAccountDeclaration(account_name="claude", tool="claude-login"),
            ModelLaneAccountDeclaration(account_name="codex", tool="codex-login"),
        ),
    )


class FakeCapacityReader:
    """A ``ProtocolHostReader`` that reports a fixed machine."""

    def __init__(self, *, load1: float = 1.5, tools: Sequence[str] = ("git",)) -> None:
        self.load1 = load1
        self.tools = tuple(tools)

    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=32,
            load1=self.load1,
            mem_available_bytes=40 * GIB,
            tools=tuple(t for t in tools if t in self.tools),
            mem_total_bytes=64 * GIB,
            swap_in_kb_s=0.0,
            swap_out_kb_s=0.0,
        )


class FakeFactsReader:
    """A ``ProtocolHostFactsReader`` that answers from a list: each item is a ``ModelHostFacts`` or an exception to raise."""

    def __init__(self, *answers: Any) -> None:
        self._answers = list(answers)
        self.reads = 0

    def read(self, overlay: object, targets: object) -> ModelHostFacts:
        answer = self._answers[min(self.reads, len(self._answers) - 1)]
        self.reads += 1
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, ModelHostFacts)
        return answer


def table_for(host: str = HOST, *, available: bool = True) -> Any:
    """A lab host table with a row for ``host`` and, because the table requires one desired-publisher, one for a publisher.

    A row that is unavailable carries no roles (the table refuses roles on one).
    """
    from omnimarket.models.overlay_keys.model_overlay_documents import (
        ModelLabHostRow,
        ModelLabHostsTable,
    )

    publisher = ModelLabHostRow(
        name="publisher.test",
        target="publisher.test",
        roles=("lab", "desired-publisher"),
    )
    this = ModelLabHostRow(
        name=host,
        target=host,
        roles=("lab",) if available else None,
        available=available,
        unavailable_reason=None if available else "in repair",
        model_port=8000,
    )
    return ModelLabHostsTable(hosts=(publisher, this))
