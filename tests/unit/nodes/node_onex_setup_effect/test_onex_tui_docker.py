"""The /docker tab (OMN-26689): listed from tui_design.yaml, one plain sentence
when Docker is missing or stopped, Start runs the setup command the design names."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from textual.pilot import Pilot

from omnimarket.nodes.node_onex_setup_effect.cli_tui import OnexTui
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _app,
    _Runner,
    _type,
)

pytestmark = pytest.mark.unit

_SIZES = [(80, 24), (140, 40)]


@pytest.mark.parametrize("size", _SIZES)
async def test_docker_is_in_the_palette_and_the_tab_bar_from_the_design(
    size: tuple[int, int],
) -> None:
    """AC1: the design's data lists it; no code does."""
    design = tui_design()
    app = _app(_Runner([]))
    async with app.run_test(size=size) as pilot:
        await pilot.press("slash")
        shown = [str(o.prompt).split()[0] for o in app.query_one("#palette").options]
        assert "/docker" in shown
    app = _app(_Runner([]))
    async with app.run_test(size=size) as pilot:
        await _type(pilot, "/docker")
        await pilot.press("enter")
        for _ in range(8):
            await pilot.pause()
        labels = [spec.label.strip() for spec in design.tabs]
        assert "Docker" in labels
        assert app.screen.query_one("#tab-docker") is not None


@pytest.mark.parametrize(
    ("stderr", "cause"),
    [
        ("docker: command not found", "not installed"),
        (
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
            "not running",
        ),
    ],
)
async def test_start_names_the_cause_without_raw_error_text(
    stderr: str, cause: str
) -> None:
    """AC2."""
    runner = _Runner([], code=1, errors=stderr)
    app = _app(runner)
    async with app.run_test(size=(120, 40)) as pilot:
        await _type(pilot, "/docker")
        await pilot.press("enter")
        for _ in range(8):
            await pilot.pause()
        app.screen.query_one("#xstart").press()
        for _ in range(8):
            await pilot.pause()
        note = str(app.screen.query_one("#xnote").render())
        assert cause in note
        assert stderr not in note
        assert "\n" not in note.strip()
        assert runner.calls


_STACK = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_onex_setup_effect/stack"
)


async def test_start_runs_setup_with_the_shipped_files_and_none_typed() -> None:
    """AC3: the exact argv the runner received."""
    runner = _Runner([], code=0)
    app = _app(runner)
    async with app.run_test(size=(120, 40)) as pilot:
        await _type(pilot, "/docker")
        await pilot.press("enter")
        for _ in range(8):
            await pilot.pause()
        app.screen.query_one("#xstart").press()
        for _ in range(8):
            await pilot.pause()
    assert (
        "setup",
        "--path",
        "docker",
        "--no-interactive",
        "--compose-file",
        str(_STACK / "compose.yaml"),
        "--topology-file",
        str(_STACK / "topology.yaml"),
    ) in runner.calls


def test_the_shipped_topology_is_valid_and_selects_a_local_service() -> None:
    from omnibase_core.models.core.model_deployment_topology import (
        ModelDeploymentTopology,
    )

    topology = ModelDeploymentTopology.model_validate(
        yaml.safe_load((_STACK / "topology.yaml").read_text())
    )
    compose = yaml.safe_load((_STACK / "compose.yaml").read_text())
    local = topology.local_services()
    assert local
    for service in local.values():
        assert service.local is not None
        assert service.local.compose_service in compose["services"]


def test_every_published_port_in_the_shipped_compose_binds_loopback() -> None:
    """AC5."""
    compose = yaml.safe_load((_STACK / "compose.yaml").read_text())
    published = [
        port
        for service in compose["services"].values()
        for port in service.get("ports", [])
    ]
    assert published
    for port in published:
        assert str(port).startswith("127.0.0.1:"), port


_ROWS = (
    '{"stack": [{"name": "postgres", "state": "running"},'
    ' {"name": "redpanda", "state": "starting"},'
    ' {"name": "worker", "state": "exited"}]}'
)


async def _open_docker[T](app: OnexTui, pilot: Pilot[T]) -> None:
    await _type(pilot, "/docker")
    await pilot.press("enter")
    for _ in range(8):
        await pilot.pause()


async def test_tab_shows_each_services_state_and_refreshes_after_start_and_stop() -> (
    None
):
    """AC4: fixture status rows for running, starting and exited services."""
    status = ("stack", "status")
    runner = _Runner([], code=0, by_command={status: _ROWS})
    app = _app(runner)
    async with app.run_test(size=(120, 40)) as pilot:
        await _open_docker(app, pilot)
        shown = str(app.screen.query_one("#xstate").render())
        for name, state in (
            ("postgres", "running"),
            ("redpanda", "starting"),
            ("worker", "exited"),
        ):
            assert name in shown
            assert state in shown
        reads = sum(1 for call in runner.calls if call[:2] == status)
        assert reads >= 1
        app.screen.query_one("#xstart").press()
        for _ in range(8):
            await pilot.pause()
        after_start = sum(1 for call in runner.calls if call[:2] == status)
        assert after_start > reads
        app.screen.query_one("#xstop").press()
        for _ in range(8):
            await pilot.pause()
        assert ("stack", "stop") in runner.calls
        assert sum(1 for call in runner.calls if call[:2] == status) > after_start
