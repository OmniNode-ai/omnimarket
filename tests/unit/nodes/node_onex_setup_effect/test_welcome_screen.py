"""OMN-26620: the welcome screen (operator's reference image, 2026-11-25).

A roomy terminal shows a large pixel ONEX wordmark, the brand line, the tagline and
four cards (Delegate, Models, Config, History) above a rounded prompt box. A small
terminal keeps the compact header so the prompt always fits. A card is clickable.
The welcome steps aside once the reader submits something.

Each test was red on the previous head (no welcome existed).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from textual.widgets import Input

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
    welcome_renderable,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design


async def _ready() -> ModelTuiStatus:
    return ModelTuiStatus(model="Qwen3.8-27B", delegation="off", models=5)


class _Quiet:
    """A runner that answers every command with an empty success."""

    async def capture(
        self, argv: tuple[str, ...], stdin: str | None = None
    ) -> tuple[int, str, str]:
        return 0, "{}", ""

    async def stream(
        self,
        argv: tuple[str, ...],
        emit: Callable[[str], None],
        stdin: str | None = None,
    ) -> int:
        emit("{}")
        return 0


def _rows(app: OnexTui) -> list[str]:
    return [
        "".join(segment.text for segment in strip).rstrip()
        for strip in app.screen._compositor.render_strips()
    ]


async def _shown(width: int, height: int) -> tuple[list[str], dict[str, object]]:
    """The rows on screen and facts read while the app is still running."""
    app = OnexTui(load_status=_ready, runner=_Quiet())
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.pause()
        await pilot.pause()
        facts: dict[str, object] = {
            "welcome": app.query_one("#welcome").display,
            "prompt_bottom": app.query_one("#prompt", Input).region.bottom,
        }
        return _rows(app), facts


async def test_a_roomy_terminal_shows_the_wordmark_brand_line_and_headline() -> None:
    rows, _ = await _shown(140, 40)
    text = "\n".join(rows)
    assert sum("█" in row for row in rows) >= 5, "the large wordmark is missing"
    assert "OmniNode Delegation" in text, "the brand line is missing"
    assert "Build more. Spend less." in text
    assert "The right intelligence, at the right cost." in text


async def test_the_brand_line_carries_the_version_once() -> None:
    rows, _ = await _shown(140, 40)
    brand = [row for row in rows if "OmniNode Delegation" in row]
    assert len(brand) == 1, brand
    assert re.search(r"v\d+\.\d+\.\d+", brand[0]), (
        "the version is not on the brand line"
    )
    assert len(re.findall(r"v\d+\.\d+\.\d+", "\n".join(rows))) == 1


async def test_the_routing_diagram_shows_the_task_the_hub_and_three_outcomes() -> None:
    rows, _ = await _shown(140, 40)
    text = "\n".join(rows)
    for word in (
        "Your Task",
        "ONEX",
        "Smart Routing",
        "Lower Cost",
        "Faster Results",
        "Right Model",
    ):
        assert word in text, word
    where = {
        word: next(i for i, row in enumerate(rows) if word in row)
        for word in ("Lower Cost", "Faster Results", "Right Model")
    }
    assert where["Lower Cost"] < where["Faster Results"] < where["Right Model"]
    diagram = rows[where["Lower Cost"] - 1 : where["Right Model"] + 2]
    assert "\n".join(diagram).count("●") <= 1, "only the travelling dot is drawn"
    task_row = next(i for i, row in enumerate(rows) if "Your Task" in row)
    assert task_row == where["Faster Results"], "the task joins the middle outcome row"


async def test_the_welcome_has_no_cards() -> None:
    """Operator feedback: the four cards are gone."""
    rows, _ = await _shown(140, 40)
    text = "\n".join(rows)
    for gone in ("View and manage", "Run a task with the best", "See past runs"):
        assert gone not in text, gone


async def test_a_small_terminal_keeps_the_compact_header() -> None:
    rows, facts = await _shown(80, 24)
    text = "\n".join(rows)
    assert "Build more. Spend less." not in text
    assert "View and manage" not in text
    assert facts["welcome"] is False
    assert "Ask onex to delegate a task" in text, "the prompt must stay on screen"


async def test_the_prompt_stays_on_screen_at_the_roomy_and_the_smallest_size() -> None:
    for width, height in ((100, 30), (140, 40), (80, 24)):
        _, facts = await _shown(width, height)
        assert facts["prompt_bottom"] <= height, (width, height, facts)


async def test_the_welcome_stays_after_something_is_submitted() -> None:
    app = OnexTui(load_status=_ready, runner=_Quiet())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.query_one("#welcome").display is True
        await pilot.press(*"/clear", "enter")
        await pilot.pause()
        await pilot.pause()
        assert app.query_one("#welcome").display is True
        assert app.query_one("#header").display is False
        assert app.query_one("#summary").display is False


def test_the_connector_dots_move_from_one_tick_to_the_next() -> None:
    frames = [welcome_renderable(tick).plain for tick in range(4)]
    assert len(set(frames)) == 4, "the dots do not move"


async def test_the_diagram_pulses_on_screen_without_a_keypress() -> None:
    app = OnexTui(load_status=_ready, runner=_Quiet())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        before = "\n".join(_rows(app))
        await pilot.pause(0.8)
        after = "\n".join(_rows(app))
    assert before != after, "the screen did not change on its own"


def test_the_version_on_the_brand_line_has_no_build_label() -> None:
    brand = next(
        line
        for line in welcome_renderable(0).plain.splitlines()
        if "OmniNode Delegation" in line
    )
    assert "+demo" not in brand


def _gold_chars(text: object, index: int) -> bool:
    """Whether the character at ``index`` of a rich Text carries the brand colour."""
    gold = tui_design().theme.primary.lower()
    for span in text.spans:
        if span.start <= index < span.end and gold in str(span.style).lower():
            return True
    return False


def _box_is_lit(text: object, word: str) -> bool:
    plain = text.plain
    return _gold_chars(text, plain.index(word))


def _frames(count: int) -> list[object]:
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import _draw_diagram

    return [_draw_diagram(tui_design(), tick) for tick in range(count)]


def test_each_outcome_box_lights_up_in_turn_and_never_two_at_once() -> None:
    words = ("Lower Cost", "Faster Results", "Right Model")
    lit_ever = dict.fromkeys(words, False)
    for frame in _frames(24 * 3 + 2):
        lit = [word for word in words if _box_is_lit(frame, word)]
        assert len(lit) <= 1, lit
        for word in lit:
            lit_ever[word] = True
    assert all(lit_ever.values()), lit_ever


def test_the_dot_reaches_a_box_before_the_box_lights_up() -> None:
    words = ("Lower Cost", "Faster Results", "Right Model")
    frames = _frames(24 * 3 + 2)
    for word in words:
        first = next(i for i, f in enumerate(frames) if _box_is_lit(f, word))
        before = frames[first - 1].plain.splitlines()
        word_row = next(
            i for i, row in enumerate(frames[first].plain.splitlines()) if word in row
        )
        # a dot sits on that box's row, just left of the box, one tick earlier
        assert "●" in before[word_row], (word, before[word_row])


def test_at_rest_no_box_is_lit_while_the_dot_is_still_on_the_first_connector() -> None:
    frame = _frames(1)[0]
    for word in ("Lower Cost", "Faster Results", "Right Model"):
        assert not _box_is_lit(frame, word), word


async def test_there_is_no_fixed_dot_while_the_dots_travel() -> None:
    """Operator feedback: a dot that never moved looked wrong beside the moving one."""
    for frame in _frames(24):
        plain = frame.plain
        assert plain.count("●") <= 1, plain


async def test_the_diagram_has_rounded_boxes_a_heavy_hub_arrowheads_and_a_hub_line() -> (
    None
):
    rows, _ = await _shown(140, 40)
    text = "\n".join(rows)
    words = tui_design().welcome.diagram
    first = next(i for i, row in enumerate(rows) if "Lower Cost" in row)
    last = next(i for i, row in enumerate(rows) if "Right Model" in row)
    diagram = "\n".join(rows[first - 1 : last + 2])
    assert diagram.count("╭") >= 4, (
        "the task and the three outcomes have rounded corners"
    )
    assert "┏" in text, "the ONEX hub has a heavy frame"
    assert diagram.count("▶") == 4, "an arrowhead into the hub and into each outcome"
    assert words.hub_tagline in text
    for icon in words.outcome_icons:
        assert icon in text, icon


async def test_a_pulse_after_the_app_closed_does_nothing() -> None:
    """The pulse timer can fire once every screen is gone, while the app closes
    (seen as an IndexError in a full suite run on 2026-11-26); it must not raise."""
    app = OnexTui(load_status=_ready, runner=_Quiet())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
    assert not app.screen_stack
    app._pulse_welcome()
