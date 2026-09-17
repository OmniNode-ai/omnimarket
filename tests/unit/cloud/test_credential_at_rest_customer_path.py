# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Credential-at-rest contract for the dashboard API key (OMN-16967, OMN-18422).

The store this module used to exercise was a second, omnimarket-local owner of
a block in ``~/.onex/config.yaml``. OMN-18422 removed it: the canonical store in
``omnibase_infra`` owns that file, and ``onex cloud`` reads and writes through
it, so one credential no longer has two homes.

What this module keeps is the part that is this repo's to guarantee. The
properties below are load-bearing for the CUSTOMER path specifically, and are
asserted here at the seam ``onex cloud`` actually uses rather than assumed from
the other repo's unit tests:

* the key VALUE never reaches ``config.yaml`` -- only a reference does, because
  config.yaml is the file people paste into support threads;
* the secret file is 0600 the moment it exists, and a loosened one is REFUSED
  on read rather than read-with-a-warning;
* there is no ``base_url`` default anywhere on the path. An unset origin is a
  refusal, never a substituted host, because a wrong-by-default origin sends a
  live customer credential to the wrong gateway.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import click
import pytest
import yaml

from omnimarket.cli.cli_cloud import _resolve_credential, _store

pytestmark = pytest.mark.unit

_KEY = "onxk_livecustomerkey"  # pragma: allowlist secret
_BASE_URL = "https://dev.api.omninode.ai"
_TENANT = "default"


def _config(root: Path) -> dict[str, object]:
    return yaml.safe_load((root / "config.yaml").read_text())


def _secrets(root: Path) -> dict[str, str]:
    return json.loads((root / "credentials.json").read_text())


def test_save_writes_a_reference_to_config_and_the_value_only_to_the_secret_file(
    tmp_path: Path,
) -> None:
    _store(tmp_path).save_api_key(tenant_slug=_TENANT, api_key=_KEY, base_url=_BASE_URL)

    assert _KEY not in (tmp_path / "config.yaml").read_text()
    assert _KEY in _secrets(tmp_path).values()

    block = _config(tmp_path)["gateway"]
    assert isinstance(block, dict)
    assert block["base_url"] == _BASE_URL
    assert _secrets(tmp_path)[block["api_key_ref"]] == _KEY


def test_the_secret_file_is_owner_only_from_the_moment_it_exists(
    tmp_path: Path,
) -> None:
    _store(tmp_path).save_api_key(tenant_slug=_TENANT, api_key=_KEY, base_url=_BASE_URL)

    mode = stat.S_IMODE((tmp_path / "credentials.json").stat().st_mode)
    assert mode & 0o077 == 0, f"credentials.json is mode {mode:04o}, not owner-only"


def test_save_preserves_other_blocks_in_config(tmp_path: Path) -> None:
    """Storing a key must not be a way to lose someone's other settings."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"kafka": {"bootstrap_servers": "example:9092"}})
    )

    _store(tmp_path).save_api_key(tenant_slug=_TENANT, api_key=_KEY, base_url=_BASE_URL)

    document = _config(tmp_path)
    assert document["kafka"] == {"bootstrap_servers": "example:9092"}
    assert "gateway" in document


def test_the_customer_path_round_trips_the_saved_credential(tmp_path: Path) -> None:
    _store(tmp_path).save_api_key(tenant_slug=_TENANT, api_key=_KEY, base_url=_BASE_URL)

    base_url, api_key = _resolve_credential(
        onex_home=tmp_path, base_url=None, api_key_file=None
    )

    assert base_url == _BASE_URL
    assert api_key.get_secret_value() == _KEY


def test_the_customer_path_refuses_a_group_readable_secret_file(
    tmp_path: Path,
) -> None:
    """A loosened key file is refused on read, not read with a warning."""
    _store(tmp_path).save_api_key(tenant_slug=_TENANT, api_key=_KEY, base_url=_BASE_URL)
    (tmp_path / "credentials.json").chmod(0o644)

    with pytest.raises(click.ClickException):
        _resolve_credential(onex_home=tmp_path, base_url=None, api_key_file=None)


def test_the_customer_path_with_no_config_names_the_command_that_creates_one(
    tmp_path: Path,
) -> None:
    with pytest.raises(click.ClickException) as excinfo:
        _resolve_credential(
            onex_home=tmp_path / "absent", base_url=None, api_key_file=None
        )

    assert "login" in str(excinfo.value)


def test_no_base_url_default_is_substituted_for_an_absent_one(tmp_path: Path) -> None:
    """An unset origin refuses; it never resolves to a host nobody chose."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {"gateway": {"tenant_slug": _TENANT, "api_key_ref": "default-api-key"}}
        )
    )
    secrets = tmp_path / "credentials.json"
    secrets.write_text(json.dumps({"default-api-key": _KEY}))
    secrets.chmod(stat.S_IRUSR | stat.S_IWUSR)

    with pytest.raises(click.ClickException):
        _resolve_credential(onex_home=tmp_path, base_url=None, api_key_file=None)
