# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Credential-at-rest contract for the dashboard API key (OMN-16967).

Two properties are load-bearing and are asserted rather than assumed:

* the key VALUE never reaches ``config.yaml`` — only a reference does, because
  config.yaml is the file people paste into support threads;
* the secret file is 0600 the moment it exists, and a loosened one is REFUSED
  on read rather than read-with-a-warning.

There is no ``base_url`` default anywhere in the store. An unset origin is a
refusal, never a substituted host.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
import yaml
from omnibase_core.errors.model_onex_error import ModelOnexError

from omnimarket.cloud.store_tenant_api_credential import StoreTenantApiCredential

pytestmark = pytest.mark.unit

_KEY = "onxk_livecustomerkey"


def _store(root: Path) -> StoreTenantApiCredential:
    return StoreTenantApiCredential(onex_home=root)


def test_save_writes_a_reference_to_config_and_the_value_only_to_the_secret_file(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.save(base_url="https://dev.api.omninode.ai", api_key=_KEY, profile="default")

    config_text = store.config_path.read_text()
    assert _KEY not in config_text

    block = yaml.safe_load(config_text)["cloud"]
    assert block["base_url"] == "https://dev.api.omninode.ai"
    assert block["api_key_ref"] == "default-cloud-api-key"
    assert "api_key" not in block

    secrets = json.loads(store.credentials_path.read_text())
    assert secrets["default-cloud-api-key"] == _KEY


def test_the_secret_file_is_owner_only_from_the_moment_it_exists(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.save(base_url="https://x", api_key=_KEY, profile="default")

    mode = stat.S_IMODE(store.credentials_path.stat().st_mode)
    assert mode == 0o600


def test_save_preserves_other_blocks_in_config(tmp_path: Path) -> None:
    """``onex cloud login`` must not be a way to lose someone's gateway config."""
    store = _store(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    store.config_path.write_text(yaml.safe_dump({"gateway": {"tenant": "acme"}}))

    store.save(base_url="https://x", api_key=_KEY, profile="default")

    document = yaml.safe_load(store.config_path.read_text())
    assert document["gateway"] == {"tenant": "acme"}
    assert document["cloud"]["api_key_ref"] == "default-cloud-api-key"


def test_load_round_trips_the_saved_credential(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(base_url="https://dev.api.omninode.ai", api_key=_KEY, profile="staging")

    credential = store.load()

    assert credential.base_url == "https://dev.api.omninode.ai"
    assert credential.profile == "staging"
    assert credential.api_key.get_secret_value() == _KEY


def test_load_refuses_a_group_or_world_readable_secret_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(base_url="https://x", api_key=_KEY, profile="default")
    store.credentials_path.chmod(0o644)

    with pytest.raises(ModelOnexError) as excinfo:
        store.load()

    assert "0600" in str(excinfo.value)


def test_load_refuses_an_inline_api_key_in_config(tmp_path: Path) -> None:
    """A key VALUE in config.yaml is refused outright, not accepted-with-a-warning."""
    store = _store(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    store.config_path.write_text(
        yaml.safe_dump(
            {
                "cloud": {
                    "base_url": "https://x",
                    "api_key_ref": "r",
                    "profile": "default",
                    "api_key": _KEY,
                }
            }
        )
    )

    with pytest.raises(ModelOnexError) as excinfo:
        store.load()

    assert "inline 'api_key'" in str(excinfo.value)


def test_load_with_no_config_names_the_command_that_creates_one(
    tmp_path: Path,
) -> None:
    with pytest.raises(ModelOnexError) as excinfo:
        _store(tmp_path).load()

    assert "onex cloud login" in str(excinfo.value)


def test_clear_removes_the_key_before_the_reference(tmp_path: Path) -> None:
    """A crash between the two writes must leave a loud refusal, not an orphan key."""
    store = _store(tmp_path)
    store.save(base_url="https://x", api_key=_KEY, profile="default")

    store.clear()

    assert json.loads(store.credentials_path.read_text()) == {}
    assert "cloud" not in yaml.safe_load(store.config_path.read_text())
