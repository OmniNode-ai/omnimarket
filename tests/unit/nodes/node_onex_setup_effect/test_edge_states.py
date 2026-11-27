"""OMN-26620: states a real terminal showed wrong (80x24 sweep, 2026-11-25).

E1  with Auto saved, the next start says "reading your setup…" beside a saved
    "delegation off": the model count was not kept, so the bar could not seed.
E2  /delegation says the hook is "not read yet" after the status read landed:
    status said the hook step needs action (no plugin) and gave no evidence.
E3  /dashboard shows an empty address row while the dashboard is stopped.
E4  /model in an 80-column window cuts off the Status column.
E5  /model in an 80-column window shows a stray scrollbar end (a lone "▎")
    inside the box: the horizontal bar's track is invisible in ANSI colour.
E6  /model in an 80-column window shortens two copies of one model on two
    servers to the same words, cutting off the server that tells them apart.
"""

from __future__ import annotations

import json

from textual.widgets import Static

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
    StatusStore,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _app,
    _root,
    _Runner,
    _selected_rows,
    _type,
)


async def test_a_saved_auto_setup_seeds_the_bar_without_reading() -> None:
    """E1."""
    StatusStore.save(ModelTuiStatus(model=None, delegation="off", models=3))

    async def never() -> ModelTuiStatus:
        import asyncio

        await asyncio.Event().wait()
        raise AssertionError

    app = OnexTui(root=_root, runner=_Runner([]), load_status=never)
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = " ".join(str(app.query_one("#status", Static).render()).split())
        assert tui_design().words["status_reading"] not in bar, bar
        assert "3 models" in bar, bar


def _long_named() -> dict[tuple[str, ...], str]:
    """Two servers by address: names as long as a real setup shows them."""
    rows = _selected_rows(connection=False)
    servers = [
        {
            "connection_id": f"lab-{n}",
            "provider": "openai_compatible",
            "url": f"http://192.0.2.{n}:8000",
            "models": ["Qwen3.8-27B"],
            "native_messages": True,
        }
        for n in (94, 95)
    ]
    rows[("models", "connections")] = json.dumps(
        {"status": "ready", "connections": servers}
    )
    return rows


async def _tab_rows(
    command: str, size: tuple[int, int], probe: str = "", *, long: bool = False
) -> list[str]:
    by_command = _long_named() if long else _selected_rows(connection=False)
    app = _app(_Runner([], by_command=by_command))
    async with app.run_test(size=size) as pilot:
        await _type(pilot, command)
        await pilot.press("enter")
        for _ in range(12):
            await pilot.pause(0.05)
        rows = [
            "".join(segment.text for segment in strip)
            for strip in app.screen._compositor.render_strips()
        ]
        if probe:
            rows.append(f"probe-display={app.screen.query_one(probe).display}")
        return rows


# `onex delegation status --json` on a lab host, 2026-11-25 (plugin not installed)
_OFF_NO_PLUGIN = {
    "status": "off",
    "claude_code": "installed",
    "switches": {"prompts": "on"},
    "observations": [
        {"step": "claude-plugin", "status": "unavailable", "evidence": {}},
        {"step": "claude-hooks", "status": "action_required", "evidence": {}},
    ],
}


def test_a_hook_step_that_needs_action_reads_as_not_installed() -> None:
    """E2."""
    assert cli_tui._hook_presence(_OFF_NO_PLUGIN) == {"prompt_hook": "missing"}


async def test_a_stopped_dashboard_has_no_empty_address_row() -> None:
    """E3."""
    rows = await _tab_rows("/dashboard", (140, 40), probe="#baddr")
    assert rows[-1] == "probe-display=False", rows[-1]


async def test_an_80_column_model_tab_still_shows_each_status() -> None:
    """E4."""
    rows = await _tab_rows("/model", (80, 24), long=True)
    header = [r for r in rows if "Model" in r and "Context" in r]
    assert len(header) == 1, "\n".join(rows)
    assert "Status" in header[0], header[0]
    # every status in full: "not tested y" is not one
    untested = [r for r in rows if "not tested" in r]
    assert untested, "\n".join(rows)
    for row in untested:
        assert "not tested yet" in row, row


async def test_an_80_column_model_tab_has_no_stray_scrollbar_glyph() -> None:
    """E5."""
    rows = await _tab_rows("/model", (80, 24), long=True)
    stray = [r for r in rows if any(g in r for g in "▏▎▍▌▋▊▉")]
    assert not stray, "\n".join(rows)


async def test_an_80_column_model_tab_keeps_two_servers_apart() -> None:
    """E6."""
    from textual.widgets import DataTable

    from omnimarket.models.model_local_server_details import ModelLocalServerDetails

    servers = ("http://192.0.2.10:8000", "http://198.51.100.20:8000")
    rows = json.dumps(
        {
            "provider": "local",
            "set_up": True,
            "models": ["Qwen3.8-27B"],
            "local_models": [
                {"model": "Qwen3.8-27B", "base_url": url, "backend_id": f"local-{n}"}
                for n, url in enumerate(servers)
            ],
        }
    )

    def read_server(
        base: str, models: tuple[str, ...]
    ) -> dict[str, ModelLocalServerDetails]:
        return {"Qwen3.8-27B": ModelLocalServerDetails(server="vLLM")}

    async def status() -> ModelTuiStatus:
        return ModelTuiStatus(models=2, delegation="off")

    app = OnexTui(
        root=_root,
        runner=_Runner([], by_command={("models", "list"): rows}),
        load_status=status,
        read_server=read_server,
    )
    async with app.run_test(size=(80, 24)) as pilot:
        await _type(pilot, "/model")
        await pilot.press("enter")
        for _ in range(8):
            await pilot.pause(0.05)
        grid = app.screen.query_one("#models").query_one(DataTable)
        names = [str(grid.get_row_at(i)[0]) for i in range(grid.row_count)]
        shown = [
            "".join(segment.text for segment in strip)
            for strip in app.screen._compositor.render_strips()
        ]
    assert len(names) == 2, names
    assert names[0] != names[1], names
    for row in shown:
        if "Qwen" in row:
            assert "not tested yet" in row or "ready" in row, row
