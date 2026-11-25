"""OMN-26620: a new user's first minutes, and keys pressed while a tab opens
(real-terminal sweep, 2026-11-25).

  F1  the first tab waits for the setup read (seconds): keys pressed meanwhile
      are lost or land on the wrong screen; Esc then /delegation opens nothing.
  F2  a tab opened while the setup is read never fills in when the read lands.
  F3  a task typed with no model set up runs, and prints an internal error
      code, a file path and an exit code.
  F4  the Model tab tells a user already in the tabs to "Type /config".
  F5  /metering on a new install says "Could not read your usage", as if
      something were broken, when nothing has been delegated yet.
"""

from __future__ import annotations

import asyncio

import pytest
from click.testing import CliRunner
from textual.widgets import DataTable, Static

from omnimarket.cli.cli_metering import metering_command
from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    DelegationScreen,
    MeteringScreen,
    ModelPickScreen,
    ModelTuiStatus,
    OnexTui,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _root,
    _Runner,
    _selected_rows,
    _type,
)

# A tab that never opens hangs rather than fails: each test has a deadline.
pytestmark = pytest.mark.timeout(30)


async def _status_two() -> ModelTuiStatus:
    return ModelTuiStatus(models=2, delegation="off")


def _gated_app(gate: asyncio.Event) -> OnexTui:
    """An app whose setup read waits for ``gate``, like a slow CLI."""
    app = OnexTui(
        root=_root,
        runner=_Runner([], by_command=_selected_rows(connection=False)),
        load_status=_status_two,
    )
    original = app.load_selection

    async def slow():  # type: ignore[no-untyped-def]
        await gate.wait()
        return await original()

    app.load_selection = slow  # type: ignore[method-assign]
    return app


async def _settle(pilot, times: int = 4) -> None:  # type: ignore[no-untyped-def]
    for _ in range(times):
        await pilot.pause()


async def test_a_tab_opens_at_once_and_keys_work_while_the_setup_is_read() -> None:
    """F1."""
    gate = asyncio.Event()
    app = _gated_app(gate)
    async with app.run_test(size=(120, 36)) as pilot:
        await _type(pilot, "/model")
        await pilot.press("enter")
        await _settle(pilot)
        assert isinstance(app.screen, ModelPickScreen), app.screen
        assert app.screen.query_one("#models").loading
        await pilot.press("tab")
        await _settle(pilot)
        assert isinstance(app.screen, DelegationScreen), app.screen
        gate.set()


async def test_esc_then_another_command_while_the_setup_is_read() -> None:
    """F1: /dashboard, Esc, /delegation."""
    gate = asyncio.Event()
    app = _gated_app(gate)
    async with app.run_test(size=(120, 36)) as pilot:
        await _type(pilot, "/dashboard")
        await pilot.press("enter")
        await _settle(pilot, 2)
        await pilot.press("escape")
        await _settle(pilot, 2)
        await _type(pilot, "/delegation")
        await pilot.press("enter")
        await _settle(pilot)
        assert isinstance(app.screen, DelegationScreen), app.screen
        gate.set()


async def test_the_open_tab_fills_in_when_the_read_lands() -> None:
    """F2."""
    gate = asyncio.Event()
    app = _gated_app(gate)
    async with app.run_test(size=(120, 36)) as pilot:
        await _type(pilot, "/model")
        await pilot.press("enter")
        await _settle(pilot)
        gate.set()
        await _settle(pilot, 8)
        table = app.screen.query_one("#models")
        assert not table.loading
        assert table.query_one(DataTable).row_count > 0
        note = str(app.screen.query_one("#pnote", Static).render())
        assert cli_tui._say("nothing_set_up") not in note, note


async def test_a_task_with_no_model_set_up_says_so_and_runs_nothing() -> None:
    """F3."""

    async def none_set_up() -> ModelTuiStatus:
        return ModelTuiStatus(models=0, delegation="off")

    runner = _Runner([])
    app = OnexTui(root=_root, runner=runner, load_status=none_set_up)
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(pilot)
        await _type(pilot, "hello there")
        await pilot.press("enter")
        await _settle(pilot)
        shown = " ".join(
            "".join(segment.text for segment in strip)
            for strip in app.screen._compositor.render_strips()
        )
    assert not [call for call in runner.calls if call[:1] == ("delegate",)], (
        runner.calls
    )
    assert "/config" in shown, shown
    assert "ONEX_CORE" not in shown
    assert tui_design().words["task_no_model"].split(".")[0] in " ".join(shown.split())


def test_the_model_tab_points_to_the_config_tab() -> None:
    """F4."""
    words = cli_tui._say("nothing_set_up")
    assert "Type /config" not in words, words
    assert "Config tab" in words, words


def test_the_cli_refusal_on_a_new_install_reads_as_never_used(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """F5: the real CLI's answer when nothing was ever delegated."""
    result = CliRunner().invoke(
        metering_command, ["--db", str(tmp_path / "absent.sqlite")]
    )
    assert result.exit_code != 0
    assert cli_tui.metering_never_used(result.output)
    assert not cli_tui.metering_never_used("database is locked")


async def test_metering_on_a_new_install_says_no_usage_yet(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """F5."""
    refusal = (
        CliRunner()
        .invoke(metering_command, ["--db", str(tmp_path / "absent.sqlite")])
        .output
    )
    runner = _Runner([], code=1, errors=refusal)
    app = OnexTui(root=_root, runner=runner, load_status=_status_two)
    async with app.run_test(size=(120, 36)) as pilot:
        await _type(pilot, "/metering")
        await pilot.press("enter")
        await _settle(pilot, 8)
        assert isinstance(app.screen, MeteringScreen)
        note = " ".join(str(app.screen.query_one("#mnote", Static).render()).split())
    assert cli_tui._say("problem_metering") not in note, note
    assert cli_tui._say("metering_empty") in note, note
