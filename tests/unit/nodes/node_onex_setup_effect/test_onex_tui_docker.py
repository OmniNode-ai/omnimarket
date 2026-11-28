"""The /docker tab (OMN-26689): listed from tui_design.yaml, one plain sentence
when Docker is missing or stopped, Start runs the setup command the design names."""

from __future__ import annotations

import pytest

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
        assert tuple(tui_design().docker.start) in runner.calls
