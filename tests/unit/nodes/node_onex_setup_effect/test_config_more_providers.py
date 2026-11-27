"""OMN-26620: /config offers Anthropic and GLM (z.ai) too (operator 2026-11-26:
"can we add more providers also? but make sure to reuse the code as much we
can").

Both go through the code every key provider already uses. Failure modes:
  P1  a new entry shows another provider's hint, or none, when chosen.
  P2  entering its key does not read that provider's model list (nothing is
      sent, or it is sent under another provider's name).
  P3  the backend has no model list for it, so the list says "unsupported"
      after the user typed a key.
"""

from __future__ import annotations

import pytest

from omnimarket.handlers.routing.byok_provider_backends import (
    resolve_byok_provider_backend,
)
from omnimarket.nodes.node_onex_setup_effect.cli_tui import OnexTui
from tests.unit.nodes.node_onex_setup_effect.test_onex_tui import (
    _found,
    _hosted_listing,
    _open_config,
    _root,
    _Runner,
    _status,
)

pytestmark = pytest.mark.timeout(60)

_NEW = [
    ("anthropic", "platform.claude.com/settings/keys"),
    ("glm", "z.ai/manage-apikey/apikey-list"),
]


@pytest.mark.parametrize(("provider", "key_page"), _NEW)
async def test_a_new_provider_shows_its_hint_and_lists_its_models(
    provider: str, key_page: str
) -> None:
    """P1, P2."""
    list_models, seen = _hosted_listing()
    app = OnexTui(
        root=_root, runner=_Runner([]), load_status=_status, list_models=list_models
    )
    async with app.run_test(size=(120, 50)) as pilot:
        await _open_config(pilot, app)
        screen = app.screen
        screen.query_one("#provider").value = provider
        await pilot.pause()
        assert key_page in str(screen.query_one("#note").render())
        screen.query_one("#key").value = "test-key-for-listing-0001"
        await pilot.pause(1.5)
        assert seen == [(provider, "test-key-for-listing-0001")]
        assert len(_found(app).entries) == 2


@pytest.mark.parametrize(("provider", "key_page"), _NEW)
def test_a_new_provider_has_a_model_list_to_read(provider: str, key_page: str) -> None:
    """P3."""
    backend = resolve_byok_provider_backend(provider)
    assert backend is not None
    assert backend.models_url
    assert not backend.endpoint_from_registration
    assert key_page in backend.key_page
