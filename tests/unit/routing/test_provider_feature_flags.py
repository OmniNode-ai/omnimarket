# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26688: a provider kept off by a feature flag until the flag is set.

Operator 2026-11-26, passing on a cofounder's request in the team thread: turn
Hopscotch off until an agreement with the provider is in place; "a simple
feature flag should work". Off by default, ONEX_ENABLE_HOPSCOTCH=1 turns it on.

Failure modes, each with a test:
  F1 with the flag unset the provider still shows or still routes: in the
     provider list, the Config tab's choices, the backend catalog (which mints
     the route a saved key would use), or the backend looked up by id.
  F2 the flag set does not bring every one of those back.
  F3 a key for the provider is still stored while the flag is unset (or the
     refusal comes after the store was touched).
  F4 a key saved earlier still shows in `onex models list`, which is also what
     the TUI reads for its Configured tab and its Model tab.
  F5 the flag reads a value that means "off" as on, or "on" as off.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from click.testing import CliRunner

from omnimarket.handlers import handler_model_keys as model_keys
from omnimarket.handlers.routing import (
    byok_catalog_providers,
    byok_provider_backends,
    provider_feature_flags,
)
from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.models import (
    model_setup_provider_traits,
)

FLAG = "ONEX_ENABLE_HOPSCOTCH"


def _clear() -> None:
    byok_catalog_providers.load_byok_catalog_added_providers.cache_clear()
    byok_catalog_providers.load_byok_catalog_providers.cache_clear()
    byok_provider_backends.load_byok_provider_catalog.cache_clear()
    byok_provider_backends.load_byok_plan_catalog.cache_clear()
    model_setup_provider_traits._listed_picker.cache_clear()
    provider_feature_flags.provider_flags.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_catalogs() -> Iterator[None]:
    _clear()
    yield
    _clear()


def _shown() -> dict[str, bool]:
    return {
        "provider list": "hopscotch"
        in byok_catalog_providers.load_byok_catalog_providers(),
        "config choices": "hopscotch" in {p.provider for p in cli_tui._picker()},
        "backend": byok_provider_backends.resolve_byok_provider_backend("hopscotch")
        is not None,
        "backend by id": byok_provider_backends.resolve_byok_backend_by_id(
            "byok-hopscotch"
        )
        is not None,
        "customer catalogue": "hopscotch"
        in byok_provider_backends.customer_provider_catalogue(),
    }


def test_hopscotch_is_off_until_the_flag_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1."""
    monkeypatch.delenv(FLAG, raising=False)
    assert provider_feature_flags.provider_turned_off("hopscotch") == FLAG
    assert provider_feature_flags.provider_turned_off("openai") is None  # the control
    shown = _shown()
    assert not any(shown.values()), shown


def test_the_flag_brings_it_all_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """F2."""
    monkeypatch.setenv(FLAG, "1")
    shown = _shown()
    assert all(shown.values()), shown


def test_a_key_is_refused_before_anything_is_touched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3."""
    monkeypatch.delenv(FLAG, raising=False)

    def touched(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the credential store was touched")

    monkeypatch.setattr(model_keys, "drain_pending_credential_events", touched)
    monkeypatch.setattr(model_keys, "store_key", touched)
    with pytest.raises(model_keys.ModelKeyRefusedError) as refused:
        model_keys.add_key("hopscotch", "ub_live_example", ("openai/gpt-4o-mini",))
    assert (
        str(refused.value)
        == "Hopscotch is not available in onex yet. Nothing was stored."
    )


class _Row:
    def __init__(self, provider: str) -> None:
        self.provider = provider

    def model_dump_json(self) -> str:
        return f'{{"provider": "{self.provider}"}}'


def test_a_saved_key_is_hidden_from_the_models_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F4."""
    from omnimarket.cli import cli_secret

    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.setattr(
        cli_secret, "listed", lambda _as_json: (_Row("openai"), _Row("hopscotch"))
    )
    result = CliRunner().invoke(cli_secret.list_models, ["--json"])
    assert result.exit_code == 0, result.output
    assert '"openai"' in result.output  # the control: the list still prints
    assert "hopscotch" not in result.output


@pytest.mark.parametrize(
    ("value", "off"),
    [
        ("1", False),
        ("true", False),
        ("YES", False),
        ("on", False),
        ("0", True),
        ("", True),
        ("no", True),
    ],
)
def test_the_flag_reads_on_and_off(
    monkeypatch: pytest.MonkeyPatch, value: str, off: bool
) -> None:
    """F5."""
    monkeypatch.setenv(FLAG, value)
    assert (provider_feature_flags.provider_turned_off("hopscotch") == FLAG) is off
