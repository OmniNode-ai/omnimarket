"""OMN-26620: the Setup Guide under the welcome diagram.

Four steps (Config, Model, Delegation, Dashboard) that show where the reader really is in
setup, mark the next step to do, and open the matching tab by click or by the keys 1 to 4
on an empty prompt. Every status comes from the status the TUI already reads; an unknown
status says unknown and is never guessed. Red on the previous head: there was no guide.
"""

from __future__ import annotations

from collections.abc import Callable

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
    guide_steps,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design


def _status(**kwargs: object) -> ModelTuiStatus:
    return ModelTuiStatus(**kwargs)  # type: ignore[arg-type]


def _by_id(steps: list[object]) -> dict[str, object]:
    return {step.id: step for step in steps}  # type: ignore[attr-defined]


def test_setup_with_models_and_a_model_but_delegation_off_points_at_delegation() -> (
    None
):
    steps = _by_id(
        guide_steps(_status(model="Qwen3.8-27B", delegation="off", models=5))
    )
    assert steps["config"].done is True  # type: ignore[attr-defined]
    assert "5 models ready" in steps["config"].status  # type: ignore[attr-defined]
    assert steps["model"].done is True  # type: ignore[attr-defined]
    assert "Qwen3.8-27B" in steps["model"].status  # type: ignore[attr-defined]
    assert steps["delegation"].done is False  # type: ignore[attr-defined]
    assert steps["delegation"].next is True  # type: ignore[attr-defined]
    assert [
        s.id
        for s in guide_steps(_status(model="Q", delegation="off", models=5))
        if s.next
    ] == [  # type: ignore[attr-defined]
        "delegation"
    ]


def test_a_fresh_install_points_at_config_first() -> None:
    steps = guide_steps(_status(model=None, delegation="off", models=0))
    assert [s.id for s in steps if s.next] == ["config"]  # type: ignore[attr-defined]


def test_everything_done_has_no_next_step() -> None:
    steps = guide_steps(_status(model="Q", delegation="hook", models=3))
    assert not [s for s in steps if s.next]  # type: ignore[attr-defined]


def test_an_unreadable_delegation_says_unknown_and_is_not_done() -> None:
    steps = _by_id(guide_steps(_status(model="Q", delegation="unknown", models=3)))
    delegation = steps["delegation"]
    assert delegation.done is False  # type: ignore[attr-defined]
    assert "Unknown" in delegation.status  # type: ignore[attr-defined]


def test_a_status_not_read_yet_claims_nothing() -> None:
    steps = guide_steps(None)
    assert all(not s.done for s in steps)  # type: ignore[attr-defined]
    assert not any(s.next for s in steps)  # type: ignore[attr-defined]


class _Quiet:
    """A runner that answers every command at once with an empty success."""

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


async def _ready() -> ModelTuiStatus:
    return _status(model="Qwen3.8-27B", delegation="off", models=5)


async def _screen(
    app: OnexTui, size: tuple[int, int], keys: tuple[str, ...] = ()
) -> str:
    async with app.run_test(size=size) as pilot:
        for _ in range(4):
            await pilot.pause()
        if keys:
            await pilot.press(*keys)
            for _ in range(4):
                await pilot.pause()
        return "\n".join(
            "".join(segment.text for segment in strip)
            for strip in app.screen._compositor.render_strips()
        )


async def test_a_tall_terminal_shows_the_guide_under_the_diagram() -> None:
    text = await _screen(OnexTui(load_status=_ready), (140, 46))
    title = tui_design().welcome.guide.title
    assert title in text
    for word in ("Config", "Model", "Delegation", "Dashboard", "5 models ready"):
        assert word in text, word
    assert text.index(title) < text.index("Smart Routing"), "the guide comes first"
    assert "press 3" in text.lower(), "the next step names its key"


async def test_a_shorter_terminal_has_the_diagram_but_no_guide() -> None:
    text = await _screen(OnexTui(load_status=_ready), (140, 38))
    assert "Right Model" in text
    assert tui_design().welcome.guide.title not in text


async def test_clicking_a_step_opens_its_tab() -> None:
    app = OnexTui(load_status=_ready, runner=_Quiet())  # type: ignore[arg-type]
    async with app.run_test(size=(140, 46)) as pilot:
        for _ in range(4):
            await pilot.pause()
        await pilot.click("#guide-model")
        for _ in range(8):
            await pilot.pause(0.1)
        assert type(app.screen).__name__ == "ModelPickScreen"


async def test_a_number_on_an_empty_prompt_opens_the_step_and_in_a_task_it_is_text() -> (
    None
):
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(140, 46)) as pilot:
        for _ in range(4):
            await pilot.pause()
        await pilot.press("3")
        for _ in range(4):
            await pilot.pause()
        assert type(app.screen).__name__ == "DelegationScreen"
    typed = OnexTui(load_status=_ready)
    async with typed.run_test(size=(140, 46)) as pilot:
        for _ in range(4):
            await pilot.pause()
        await pilot.press("a", "3")
        assert typed.query_one("#prompt").value == "a3"  # type: ignore[attr-defined]
        assert type(typed.screen).__name__ == "Screen"


async def test_the_guide_goes_away_with_the_diagram_once_you_start() -> None:
    text = await _screen(OnexTui(load_status=_ready), (140, 46), (*"/clear", "enter"))
    assert tui_design().welcome.guide.title not in text
    assert "Smart Routing" not in text


async def test_the_guide_arrows_are_still() -> None:
    """Operator feedback: the Setup Guide is static; only the diagram moves."""
    app = OnexTui(load_status=_ready)
    async with app.run_test(size=(140, 46)) as pilot:
        for _ in range(4):
            await pilot.pause()
        seen = set()
        for _ in range(5):
            seen.add("".join(str(w.render()) for w in app.query(".guide-arrow")))
            await pilot.pause(0.25)
        assert len(seen) == 1, "the guide arrows change"
        assert "●" not in next(iter(seen))
