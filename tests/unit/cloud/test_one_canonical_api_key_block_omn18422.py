# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One on-disk block carries the tenant API key (OMN-18422).

Two commands used to write the same credential kind -- a dashboard-minted
``onxk_`` key plus the gateway origin it is presented to -- into the same
``~/.onex/config.yaml`` under two different top-level names. ``onex auth login
--api-key-stdin`` wrote ``gateway:``; ``onex cloud login`` wrote ``cloud:``.
Neither reader could see the other's block, so a machine onboarded through one
command was told by the other that it held no key at all.

These tests pin the single-block invariant, the one-shot migration of a machine
that still carries the legacy block, and the refusals that must survive it: a
machine holding a confidential-client credential is still refused rather than
resolved, and a machine carrying BOTH blocks is named rather than silently
resolved by precedence.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
import yaml
from omnibase_core.errors.model_onex_error import ModelOnexError

from omnimarket.cloud.migrate_legacy_cloud_block import (
    LEGACY_CLOUD_BLOCK,
    migrate_legacy_cloud_block,
)

pytestmark = pytest.mark.unit

_BASE_URL = "https://dev.api.omninode.ai"
_TENANT = "acme"
_KEY_REF = "acme-api-key"  # pragma: allowlist secret
_KEY_VALUE = "onxk_not_a_real_key"  # pragma: allowlist secret


def _write_config(root: Path, document: dict[str, object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(yaml.safe_dump(document, sort_keys=False))


def _write_secrets(root: Path, entries: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "credentials.json"
    path.write_text(json.dumps(entries))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _top_level_keys(root: Path) -> list[str]:
    return sorted(yaml.safe_load((root / "config.yaml").read_text()))


def _canonical_block(root: Path) -> dict[str, object]:
    document = yaml.safe_load((root / "config.yaml").read_text())
    block = document["gateway"]
    assert isinstance(block, dict)
    return block


def _seed_legacy_only(root: Path) -> None:
    """A machine onboarded through the retired ``onex cloud login`` writer."""
    _write_config(
        root,
        {
            LEGACY_CLOUD_BLOCK: {
                "base_url": _BASE_URL,
                "api_key_ref": _KEY_REF,
                "profile": _TENANT,
            }
        },
    )
    _write_secrets(root, {_KEY_REF: _KEY_VALUE})


def _seed_canonical_only(root: Path) -> None:
    """A machine onboarded through ``onex auth login --api-key-stdin``."""
    _write_config(
        root,
        {
            "gateway": {
                "tenant_slug": _TENANT,
                "api_key_ref": _KEY_REF,
                "base_url": _BASE_URL,
            }
        },
    )
    _write_secrets(root, {_KEY_REF: _KEY_VALUE})


# -- AC1 -------------------------------------------------------------------


def test_ac1_cloud_resolver_reads_the_canonical_block(tmp_path: Path) -> None:
    """The key stored by the OTHER login command resolves, instead of refusing.

    This is the reported defect in its smallest form: before the fix the cloud
    reader required its own block name and reported CONFIGURATION_NOT_FOUND on a
    machine that demonstrably held a key.
    """
    from omnimarket.cli.cli_cloud import _resolve_credential

    _seed_canonical_only(tmp_path)

    base_url, api_key = _resolve_credential(
        onex_home=tmp_path, base_url=None, api_key_file=None
    )

    assert base_url == _BASE_URL
    assert api_key.get_secret_value() == _KEY_VALUE


# -- AC2 -------------------------------------------------------------------


def test_ac2_cloud_login_writes_no_legacy_block(tmp_path: Path) -> None:
    """The cloud writer stops owning a second block name."""
    from omnimarket.cli.cli_cloud import _store

    _store(tmp_path).save_api_key(
        tenant_slug=_TENANT, api_key=_KEY_VALUE, base_url=_BASE_URL
    )

    assert _top_level_keys(tmp_path) == ["gateway"]
    assert LEGACY_CLOUD_BLOCK not in _top_level_keys(tmp_path)


def test_ac2_cloud_login_preserves_unrelated_top_level_keys(tmp_path: Path) -> None:
    """Storing a key must not be a way to lose someone's other settings."""
    from omnimarket.cli.cli_cloud import _store

    _write_config(tmp_path, {"kafka": {"bootstrap_servers": "example:9092"}})

    _store(tmp_path).save_api_key(
        tenant_slug=_TENANT, api_key=_KEY_VALUE, base_url=_BASE_URL
    )

    assert _top_level_keys(tmp_path) == ["gateway", "kafka"]


# -- AC3 -------------------------------------------------------------------


def test_ac3_legacy_block_is_migrated_once_and_removed(tmp_path: Path) -> None:
    """A machine on the legacy block is carried across exactly once."""
    _seed_legacy_only(tmp_path)

    first = migrate_legacy_cloud_block(onex_home=tmp_path)

    assert first.migrated is True
    assert first.tenant_slug == _TENANT
    assert _top_level_keys(tmp_path) == ["gateway"]
    # The secret reference is carried across unchanged, so credentials.json
    # needs no rewrite and the key is never re-read or re-written.
    assert _canonical_block(tmp_path) == {
        "tenant_slug": _TENANT,
        "api_key_ref": _KEY_REF,
        "base_url": _BASE_URL,
    }

    second = migrate_legacy_cloud_block(onex_home=tmp_path)

    assert second.migrated is False
    assert second.reason == "no_legacy_block"
    assert _top_level_keys(tmp_path) == ["gateway"]


def test_ac3_migrated_machine_then_resolves(tmp_path: Path) -> None:
    """The migration is only worth anything if the credential resolves after it."""
    from omnimarket.cli.cli_cloud import _resolve_credential

    _seed_legacy_only(tmp_path)

    base_url, api_key = _resolve_credential(
        onex_home=tmp_path, base_url=None, api_key_file=None
    )

    assert base_url == _BASE_URL
    assert api_key.get_secret_value() == _KEY_VALUE
    assert _top_level_keys(tmp_path) == ["gateway"]


def test_ac3_migration_is_a_noop_on_a_machine_with_no_config(tmp_path: Path) -> None:
    result = migrate_legacy_cloud_block(onex_home=tmp_path / "absent")

    assert result.migrated is False
    assert result.reason == "no_config"


# -- AC4 -------------------------------------------------------------------


def test_ac4_client_secret_only_machine_is_still_refused(tmp_path: Path) -> None:
    """A confidential-client credential is not an API key and must not resolve.

    The unification is of ONE credential kind. The other kind keeps its own
    refusal: minting a bearer here and presenting it as an API key is the
    conflation the retired model's docstring warned about, and that warning
    survives this change intact.
    """
    import click

    from omnimarket.cli.cli_cloud import _resolve_credential

    _write_config(
        tmp_path,
        {
            "gateway": {
                "tenant_slug": _TENANT,
                "client_id": "acme-machine",
                "client_secret_ref": "acme-client-secret",  # pragma: allowlist secret
                "token_endpoint": "https://auth.example/token",
                "base_url": _BASE_URL,
                "edge_instance_id": "edge-1",
            }
        },
    )
    _write_secrets(tmp_path, {"acme-client-secret": "not-an-api-key"})

    with pytest.raises(click.ClickException) as excinfo:
        _resolve_credential(onex_home=tmp_path, base_url=None, api_key_file=None)

    message = str(excinfo.value)
    assert "client-credential" in message
    assert "onex cloud login" in message


def test_ac4_both_blocks_present_is_named_not_resolved_by_precedence(
    tmp_path: Path,
) -> None:
    """Two homes for one credential is the defect; refuse rather than pick.

    Silently preferring one would reintroduce exactly the failure this ticket
    closes, with the loser's key live on disk and unread.
    """
    _write_config(
        tmp_path,
        {
            "gateway": {
                "tenant_slug": _TENANT,
                "api_key_ref": _KEY_REF,
                "base_url": _BASE_URL,
            },
            LEGACY_CLOUD_BLOCK: {
                "base_url": "https://other.example",
                "api_key_ref": "other-api-key",  # pragma: allowlist secret
                "profile": "other",
            },
        },
    )
    _write_secrets(tmp_path, {_KEY_REF: _KEY_VALUE})

    with pytest.raises(ModelOnexError) as excinfo:
        migrate_legacy_cloud_block(onex_home=tmp_path)

    message = str(excinfo.value)
    assert LEGACY_CLOUD_BLOCK in message
    assert "gateway" in message
    # Nothing is rewritten on a refusal.
    assert _top_level_keys(tmp_path) == ["cloud", "gateway"]


def test_ac4_malformed_legacy_block_is_refused_not_silently_skipped(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path, {LEGACY_CLOUD_BLOCK: "not-a-mapping"})

    with pytest.raises(ModelOnexError):
        migrate_legacy_cloud_block(onex_home=tmp_path)


def test_ac4_legacy_block_missing_a_required_key_is_refused(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {LEGACY_CLOUD_BLOCK: {"base_url": _BASE_URL, "profile": _TENANT}},
    )

    with pytest.raises(ModelOnexError):
        migrate_legacy_cloud_block(onex_home=tmp_path)


# -- AC5 -------------------------------------------------------------------


def test_ac5_the_retired_surface_is_named_only_by_the_migration() -> None:
    """No steady-state read path may accept both block names.

    The migration module is the one place in the shipped package allowed to
    name the retired block or the modules that owned it. Anywhere else is a
    compatibility layer, which is this defect with a longer name.
    """
    package_root = Path(__file__).resolve().parents[3] / "src" / "omnimarket"
    assert package_root.is_dir(), package_root

    migration_module = package_root / "cloud" / "migrate_legacy_cloud_block.py"
    needles = (
        "LEGACY_CLOUD_BLOCK",
        "store_tenant_api_credential",
        "model_tenant_api_credential",
        "StoreTenantApiCredential",
        "ModelTenantApiCredential",
    )

    offenders: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        if path == migration_module:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            for needle in needles:
                if needle in line:
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")

    assert offenders == [], "retired credential surface outside the migration:\n" + (
        "\n".join(offenders)
    )


def test_ac5_positive_control_the_sweep_can_find_the_name() -> None:
    """The AC5 sweep above would be vacuous if the name existed nowhere.

    A sweep that returns zero because its needle matches nothing anywhere is
    indistinguishable from a clean bill of health, so the needle is proven to
    match in the one file that is allowed to carry it.
    """
    package_root = Path(__file__).resolve().parents[3] / "src" / "omnimarket"
    migration_source = (
        package_root / "cloud" / "migrate_legacy_cloud_block.py"
    ).read_text()

    assert "LEGACY_CLOUD_BLOCK" in migration_source
    assert LEGACY_CLOUD_BLOCK == "cloud"
