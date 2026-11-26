# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26688: /config describes the provider being added, and leads with the
well-known ones.

Operator 2026-11-26, on a /config screenshot with 258 providers: "improve
more". The add pane showed a key box over an empty half screen, so a user
picking one of 250 unfamiliar names learned nothing about it before pasting a
key; and the list opened on 302.AI, not on a provider most users know.

Failure modes:
  K1  the pane says nothing about the marked provider: how many models it
      has, what they cost, which are newest.
  K2  nothing says where the key goes, which a user pasting a key into an
      unfamiliar provider needs to know.
  K3  a provider models.dev has no models or prices for breaks the pane, or
      shows a made-up price.
  K4  the card stays up while the Your providers panel or a model list needs
      the room, or pushes the footer off at 80x24.
  K5  the well-known providers are buried in an A-Z list of 250.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from textual.widgets import Input, RadioButton

from omnimarket.handlers.model_catalog import ModelsDevIndex
from omnimarket.handlers.routing.byok_catalog_providers import (
    load_byok_catalog_providers,
)
from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.cli_tui import SetupScreen
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_provider_traits import (
    setup_provider_traits,
)
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _app,
    _Runner,
    _selected_rows,
    _type,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_MODELS = {
    ("deepseek", "deepseek-chat"): {
        "name": "DeepSeek Chat",
        "release_date": "2026-03-01",
        "cost": {"input": 0.27, "output": 1.1},
    },
    ("deepseek", "deepseek-reasoner"): {
        "name": "DeepSeek Reasoner",
        "release_date": "2026-09-15",
        "cost": {"input": 0.55, "output": 2.19},
    },
    ("deepseek", "deepseek-coder"): {
        "name": "DeepSeek Coder",
        "release_date": "2025-06-01",
        "cost": {"input": 0.14, "output": 0.28},
    },
}


@pytest.fixture(autouse=True)
def _index(monkeypatch: pytest.MonkeyPatch) -> None:
    index = ModelsDevIndex(
        models=_MODELS, fetched_at="2026-11-26T00:00:00+00:00", stale=False, error=None
    )
    monkeypatch.setattr(cli_tui, "_default_index", lambda: index)
    monkeypatch.setattr(cli_tui, "_default_read_server", lambda _base, _models: {})


async def _settle(pilot, times: int = 10) -> None:
    for _ in range(times):
        await pilot.pause()


async def _config(pilot, app) -> SetupScreen:
    await _type(pilot, "/config")
    await pilot.press("enter")
    await _settle(pilot, 6)
    assert isinstance(app.screen, SetupScreen)
    return app.screen


async def _pick(pilot, screen, name: str) -> None:
    screen.query_one("#provider-search", Input).focus()
    await pilot.pause()
    await _type(pilot, name)
    await pilot.press("enter")
    await _settle(pilot)


def _card(screen) -> str:
    card = screen.query_one("#card")
    words = [str(card.border_title)]
    words += [str(child.render()) for child in card.children if child.display]
    return " ".join(" ".join(words).split())


def _host(provider: str) -> str:
    return str(urlsplit(load_byok_catalog_providers()[provider].api).hostname)


async def test_the_card_says_what_the_provider_offers() -> None:
    """K1, K2."""
    app = _app(_Runner([]))
    async with app.run_test(size=(140, 40)) as pilot:
        screen = await _config(pilot, app)
        await _pick(pilot, screen, "deepseek")
        assert screen.query_one("#card").display
        said = _card(screen)
    assert "DeepSeek" in said, said
    assert "3 models" in said, said
    assert "$0.14 to $0.55 per 1M input tokens" in said, said
    assert "DeepSeek Reasoner, DeepSeek Chat, DeepSeek Coder" in said, said
    assert f"sent only to {_host('deepseek')}" in said, said


async def test_a_provider_with_nothing_on_models_dev_says_so() -> None:
    """K3: no count, no price, no names made up."""
    listed = sorted(set(load_byok_catalog_providers()) - {"deepseek"})
    app = _app(_Runner([]))
    async with app.run_test(size=(140, 40)) as pilot:
        screen = await _config(pilot, app)
        await _pick(pilot, screen, listed[0])
        said = _card(screen)
    assert "read once your key is in" in said, said
    assert "$" not in said, said
    assert f"sent only to {_host(listed[0])}" in said, said


async def test_the_card_steps_aside_for_your_providers() -> None:
    """K4."""
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=(140, 40)) as pilot:
        screen = await _config(pilot, app)
        assert screen.query_one("#card").display
        listing = screen.query_one("#mine")
        listing.focus()
        listing.highlighted = 0
        await _settle(pilot)
        assert not screen.query_one("#card").display


@pytest.mark.parametrize("size", [(80, 24), (140, 40)])
async def test_the_card_fits_above_the_footer(size: tuple[int, int]) -> None:
    """K4."""
    app = _app(_Runner([], by_command=_selected_rows(connection=False)))
    async with app.run_test(size=size) as pilot:
        screen = await _config(pilot, app)
        await _pick(pilot, screen, "deepseek")
        card = screen.query_one("#card").region
        assert card.height >= 3, card
        assert card.bottom <= screen.query_one("#cfg-foot").region.y
        assert card.right <= size[0]


async def test_well_known_providers_lead_the_add_list() -> None:
    """K5: the hand-written providers first, A-Z, then the listed ones A-Z."""
    app = _app(_Runner([]))
    async with app.run_test(size=(140, 40)) as pilot:
        screen = await _config(pilot, app)
        shown = [
            b.name
            for b in screen.query_one("#provider").query(RadioButton)
            if b.display
        ]
        marked = screen.query_one("#provider").value
    traits = setup_provider_traits()
    written = sorted(traits.picker, key=lambda e: e.label.casefold())
    rest = sorted(
        (
            e
            for e in traits.picker_providers()
            if e.provider not in {w.provider for w in written}
        ),
        key=lambda e: e.label.casefold(),
    )
    assert shown == [e.provider for e in (*written, *rest)]
    assert marked == written[0].provider
