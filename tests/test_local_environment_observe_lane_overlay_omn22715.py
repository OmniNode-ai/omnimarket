# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22715: the observer probes the lane's own roster, not the laptop catalog.

The dev lane's local-environment projection held two rows read DOWN with
ConnectError (consumer-health-projection and tenant-projection-writer), because
the observer resolved the contract's ``local`` catalog bundle, the laptop's
service set, on a lane that runs neither. A lane names what it runs in the
node's overlay; with an overlay the catalog bundle is never read.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.enums.enum_local_service_state import EnumLocalServiceState
from omnimarket.models.model_local_environment_status import ModelLocalEnvironmentStatus
from omnimarket.nodes.node_local_environment_observe_effect.handlers import (
    handler_local_environment_observe as observer,
)

pytestmark = pytest.mark.unit

TENANT = "19988000-0000-4000-8000-000000000001"
NOW = datetime(2026, 11, 6, 17, 0, tzinfo=UTC)
OVERLAY_ID = "node_local_environment_observe_effect"
LANE_SERVICES = [
    {"service": "omninode-runtime", "port": 8085, "scheme": "http", "path": "/health"},
    {
        "service": "omninode-runtime-effects",
        "port": 8086,
        "scheme": "http",
        "path": "/health",
    },
    {"service": "postgres", "port": 5432},
]


class _RecordingProber:
    def __init__(self) -> None:
        self.calls: list[observer.ModelProbeTarget] = []

    async def probe(
        self, target: observer.ModelProbeTarget, timeout: float
    ) -> observer.ModelProbeResult:
        self.calls.append(target)
        return observer.ModelProbeResult(
            observed_state=EnumLocalServiceState.RUNNING,
            reason=f"Probed {target.service}:{target.port}.",
        )


def _tick() -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=NOW,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=NOW,
        correlation_id=uuid4(),
        scheduler_id="omn22715-test",
        tick_interval_ms=1000,
    )


def _overlay_root(tmp_path: Path, document: object) -> dict[str, str]:
    node_dir = tmp_path / "overlays" / OVERLAY_ID
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(yaml.safe_dump(document))
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays")}


@pytest.fixture
def laptop_catalog(tmp_path: Path) -> Path:
    """A catalog whose ``local`` bundle holds the two laptop-only services."""
    catalog = tmp_path / "catalog"
    services = catalog / "services"
    services.mkdir(parents=True)
    names = ["consumer-health-projection", "tenant-projection-writer"]
    (catalog / "bundles.yaml").write_text(
        yaml.safe_dump({"local": {"services": names}})
    )
    for name in names:
        (services / f"{name}.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": name,
                    "image": "test:local",
                    "layer": "runtime",
                    "ports": {"external": 19000, "internal": 8098},
                }
            )
        )
    return catalog


@pytest.fixture(autouse=True)
def _tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observer, "_resolve_observed_tenant", lambda: TENANT)


def _handler(
    catalog: Path, prober: object, environ: dict[str, str]
) -> observer.HandlerLocalEnvironmentObserve:
    return observer.HandlerLocalEnvironmentObserve(
        prober=prober,  # type: ignore[arg-type]
        config=observer.ModelLocalEnvironmentObserveConfig(catalog_dir=str(catalog)),
        environ=environ,
    )


def _services(events: tuple[object, ...]) -> list[str]:
    return [
        event.service
        for event in events
        if isinstance(event, ModelLocalEnvironmentStatus)
    ]


@pytest.mark.asyncio
async def test_a_lane_overlay_names_the_services_probed_and_the_catalog_is_not_read(
    tmp_path: Path, laptop_catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: three services from the lane overlay, no row from the laptop bundle."""
    environ = _overlay_root(tmp_path, {"services": LANE_SERVICES})

    def _no_catalog(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the catalog was read although a lane overlay was given")

    monkeypatch.setattr(observer, "CatalogResolver", _no_catalog)
    prober = _RecordingProber()

    output = await _handler(laptop_catalog, prober, environ).handle(_tick())

    expected = ["omninode-runtime", "omninode-runtime-effects", "postgres"]
    assert _services(output.events) == expected
    assert [(t.service, t.port, t.scheme, t.path) for t in prober.calls] == [
        ("omninode-runtime", 8085, "http", "/health"),
        ("omninode-runtime-effects", 8086, "http", "/health"),
        ("postgres", 5432, None, "/"),
    ]


@pytest.mark.asyncio
async def test_with_no_lane_overlay_the_catalog_bundle_is_probed(
    tmp_path: Path, laptop_catalog: Path
) -> None:
    """AC1, other half: the laptop keeps its catalog bundle."""
    environ = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "no-overlays-here")}
    prober = _RecordingProber()

    output = await _handler(laptop_catalog, prober, environ).handle(_tick())

    assert _services(output.events) == [
        "consumer-health-projection",
        "tenant-projection-writer",
    ]


@pytest.mark.asyncio
async def test_an_unreachable_overlay_service_reads_down_with_its_reason(
    tmp_path: Path, laptop_catalog: Path
) -> None:
    """AC2: a real probe of a closed port; the row is the overlay's, never the catalog's."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    environ = _overlay_root(
        tmp_path, {"services": [{"service": "127.0.0.1", "port": closed_port}]}
    )

    output = await _handler(
        laptop_catalog, observer.RealNetworkProber(), environ
    ).handle(_tick())

    (event,) = output.events
    assert isinstance(event, ModelLocalEnvironmentStatus)
    assert event.service == "127.0.0.1"
    assert event.observed_state is EnumLocalServiceState.DOWN
    assert event.reason == (
        f"TCP connection to 127.0.0.1:{closed_port} failed (ConnectionRefusedError)."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [{"services": []}, {"services": [{"service": "x"}]}, {"bundle": "local"}, ["x"]],
)
async def test_a_broken_overlay_refuses_and_never_falls_back_to_the_catalog(
    tmp_path: Path, laptop_catalog: Path, document: object
) -> None:
    environ = _overlay_root(tmp_path, document)
    prober = _RecordingProber()

    output = await _handler(laptop_catalog, prober, environ).handle(_tick())

    assert output.events == ()
    assert prober.calls == []
    (log,) = output.logs
    assert log.startswith("Local environment observation refused:")
    assert OVERLAY_ID in log
