# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two local chat servers scenario R2 adds from its settings overlay (OMN-23602).

Each local server is pinned as ``<base url>|<model id>``. On 2026-11-10 the first model id came out
as ``qwen3.8`` -- a stored routing alias the server does not list (it lists ``Qwen3.8-27B``) -- so
the pinned delegation failed; and the second server was the retired fast address, so only one local
model was ever listed. The overlay now pins the id the server itself lists and the other lab host's
address. Two tests here are the failing controls of that change: against the old overlay (templates
over ``LLM_CODER_MODEL_NAME`` and ``LLM_CODER_FAST_URL``) they come out red.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.handlers.delegation_scenario_settings import fill_overlay_settings
from omnimarket.nodes.node_delegation_scenario_r2_effect.runtime_scenario_r2_tick import (
    main,
)
from tests.nodes.node_delegation_scenario_r2_effect.test_golden_chain_scenario_r2_effect import (
    RUNTIME,
)

pytestmark = pytest.mark.unit

#: The model ids the first local server lists, as read from it on 2026-11-10: the one id it serves.
FIRST_SERVER_LISTS = ("Qwen3.8-27B",)
FIRST_SERVER_URL = "http://127.0.0.1:8000"
STORED_MODEL_ALIAS = "qwen3.8"
RETIRED_FAST_URL = "http://127.0.0.1:8001"
SECOND_SERVER_URL = "http://127.0.0.1:8003"
RETIRED_FAST_MODEL_FILE = "Qwen3.8-27B-MTP-IQ4_XS.gguf"
NODE_DIR = Path(main.__code__.co_filename).parent


class FakeSettingStore:
    """The store seam for the overlay: the values it holds."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = {name: SecretStr(value) for name, value in values.items()}

    async def read(self, folder: str, name: str) -> SecretStr | None:
        assert folder == RUNTIME.secret_store_folder
        return self._values.get(name)


def _store() -> FakeSettingStore:
    """The store as the lab holds it: the first server's address, the alias, the retired fast one."""
    return FakeSettingStore(
        {
            "LLM_CODER_URL": FIRST_SERVER_URL,
            "LLM_CODER_MODEL_NAME": STORED_MODEL_ALIAS,
            "LLM_CODER_FAST_URL": RETIRED_FAST_URL,
            "LLM_CODER_PC2_URL": SECOND_SERVER_URL,
            "LLM_CODER_FAST_MODEL_NAME": RETIRED_FAST_MODEL_FILE,
        }
    )


def _filled(tmp_path: Path) -> dict[str, str]:
    """An empty environment the overlay filled: nothing in it, no host file, then the store."""
    return fill_overlay_settings(
        {},
        RUNTIME.settings_overlay_relpath,
        node_dir=NODE_DIR,
        store=_store(),
        home=tmp_path,
    )


def _local_server(value: str) -> tuple[str, str]:
    """``<base url>|<model id>`` split on the first pipe."""
    url, _, model = value.partition("|")
    return url, model


def test_first_local_model_is_one_the_server_lists(tmp_path: Path) -> None:
    """The first local server is pinned to a model id it lists, not the stored alias."""
    url, model = _local_server(_filled(tmp_path)[RUNTIME.local_first_env])
    assert url == FIRST_SERVER_URL
    assert model in FIRST_SERVER_LISTS


def test_the_two_local_servers_are_different_addresses(tmp_path: Path) -> None:
    """The two locals are two different addresses, and neither is the refusal leg's dead one."""
    env = _filled(tmp_path)
    first_url, _ = _local_server(env[RUNTIME.local_first_env])
    second_url, _ = _local_server(env[RUNTIME.local_second_env])
    assert second_url != first_url
    assert first_url != env[RUNTIME.dead_url_env]
    assert second_url != env[RUNTIME.dead_url_env]


def test_second_local_server_is_not_the_retired_fast_server(tmp_path: Path) -> None:
    """The second local server is on its own address, not the store's retired fast one."""
    second_url, second_model = _local_server(
        _filled(tmp_path)[RUNTIME.local_second_env]
    )
    assert second_url == SECOND_SERVER_URL
    assert second_url != RETIRED_FAST_URL
    assert second_model != RETIRED_FAST_MODEL_FILE
