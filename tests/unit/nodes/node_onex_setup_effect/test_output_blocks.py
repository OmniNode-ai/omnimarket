"""OMN-26620: the output is a stack of blocks, not an append-only text log.

Behaviour (written in our own words from the operator's request for a block
based, keyboard-navigable output):
  * every write is its own block that can be selected and copied;
  * the view follows new output, stops following when the reader scrolls away,
    and follows again when the reader submits or comes back to the prompt;
  * alt+up / alt+down walk the blocks; walking past the last block returns to
    the prompt; c copies the selected block;
  * typing while a block is selected goes to the prompt, not into the void.

Failure modes each test can catch: a log that cannot select a block; an anchor
that drags the reader back down while reading history; a cursor that traps focus
away from the prompt; a copy that copies markup instead of the words.
"""

from __future__ import annotations

from rich.markdown import Markdown
from rich.text import Text
from textual.widgets import Input

from omnimarket.nodes.node_onex_setup_effect.cli_tui import ModelTuiStatus, OnexTui


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off", models=1)


async def _started(pilot) -> None:  # type: ignore[no-untyped-def]
    await pilot.pause()
    await pilot.pause()


async def test_every_write_is_its_own_block() -> None:
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(100, 30)) as pilot:
        await _started(pilot)
        pane = app.query_one("#output")
        before = len(pane.blocks)
        pane.write("first block")
        pane.write(Text("second block"))
        pane.write(Markdown("# third\n\nbody"))
        await pilot.pause()
        assert len(pane.blocks) == before + 3
        assert [block.plain for block in pane.blocks][-3:-1] == [
            "first block",
            "second block",
        ]
        assert pane.blocks[-1].plain.startswith("# third")


async def test_clear_removes_every_block() -> None:
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(100, 30)) as pilot:
        await _started(pilot)
        pane = app.query_one("#output")
        pane.write("something")
        await pilot.pause()
        pane.clear()
        await pilot.pause()
        assert len(pane.blocks) == 0
        assert list(pane.lines) == []


async def test_lines_keep_the_text_log_reading_shape() -> None:
    """Older readers use ``.lines`` with ``str(line)`` and ``line.text``."""
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(100, 30)) as pilot:
        await _started(pilot)
        pane = app.query_one("#output")
        pane.clear()
        pane.write("alpha\nbeta")
        await pilot.pause()
        lines = list(pane.lines)
        assert [str(line) for line in lines] == ["alpha", "beta"]
        assert lines[0].text == "alpha"


async def test_the_view_follows_new_output_until_the_reader_scrolls_away() -> None:
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(100, 24)) as pilot:
        await _started(pilot)
        pane = app.query_one("#output")
        for index in range(60):
            pane.write(f"line {index}")
        await pilot.pause()
        await pilot.pause()
        assert pane.max_scroll_y > 0
        assert pane.scroll_y == pane.max_scroll_y, "the view should follow the end"
        # the reader scrolls up to read history: new output must not drag them down
        pane.scroll_to(y=0, animate=False)
        pane.release_anchor()
        pane.write("late line")
        await pilot.pause()
        await pilot.pause()
        assert pane.scroll_y == 0, "the reader was pulled back down"
        # coming back to the prompt follows the end again
        pane.follow()
        await pilot.pause()
        await pilot.pause()
        assert pane.scroll_y == pane.max_scroll_y


async def test_alt_arrows_walk_the_blocks_and_return_to_the_prompt() -> None:
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(100, 30)) as pilot:
        await _started(pilot)
        pane = app.query_one("#output")
        pane.clear()
        pane.write("one")
        pane.write("two")
        pane.write("three")
        await pilot.pause()
        assert pane.cursor is None
        await pilot.press("alt+up")
        assert pane.cursor == 2
        assert pane.blocks[2].has_class("-cursor")
        await pilot.press("alt+up")
        assert pane.cursor == 1
        assert not pane.blocks[2].has_class("-cursor")
        await pilot.press("alt+down")
        await pilot.press("alt+down")
        assert pane.cursor is None
        assert isinstance(app.focused, Input)


async def test_c_copies_the_selected_block_as_plain_words() -> None:
    app = OnexTui(load_status=_ready)
    copied: list[str] = []
    app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
    async with app.run_test(size=(100, 30)) as pilot:
        await _started(pilot)
        pane = app.query_one("#output")
        pane.clear()
        pane.write(Text("the answer is 42", style="bold green"))
        await pilot.pause()
        await pilot.press("alt+up")
        await pilot.press("c")
        assert copied == ["the answer is 42"]


async def test_typing_while_a_block_is_selected_goes_to_the_prompt() -> None:
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(100, 30)) as pilot:
        await _started(pilot)
        pane = app.query_one("#output")
        pane.write("something to select")
        await pilot.pause()
        await pilot.press("alt+up")
        assert pane.cursor is not None
        await pilot.press("x")
        assert pane.cursor is None
        prompt = app.query_one("#prompt", Input)
        assert app.focused is prompt
        assert prompt.value == "x"
