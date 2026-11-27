"""OMN-26620: an unreadable delegation status is said once and never contradicted.

Seen on a real run: the start screen said "Delegation off" while another line
said the status could not be read, and the same sentence appeared in the status
block and again in the footer.
"""

from __future__ import annotations

from omnimarket.nodes.node_onex_setup_effect.cli_tui import (
    ModelTuiStatus,
    OnexTui,
    summary_renderable,
)

_SENTENCE = "Could not read the delegation status"


def test_an_unreadable_status_does_not_read_as_off() -> None:
    status = ModelTuiStatus(model=None, delegation="unknown", models=0)
    text = summary_renderable(status).plain
    assert "Delegation off" not in text
    assert "status" in text.lower()


async def _unreadable() -> ModelTuiStatus:
    raise RuntimeError("models list could not be read")


async def test_the_unreadable_sentence_is_on_the_screen_once() -> None:
    app = OnexTui(load_status=_unreadable)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        lines: list[str] = []
        for strip in app.screen._compositor.render_strips():
            lines.append("".join(segment.text for segment in strip))
    shown = "\n".join(lines)
    assert shown.count(_SENTENCE) <= 1, shown
