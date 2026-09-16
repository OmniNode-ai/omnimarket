# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One-shot migration off the retired ``cloud:`` credential block (OMN-18422).

WHY THIS EXISTS
    Two commands used to write the SAME credential kind -- a dashboard-minted
    ``onxk_`` key plus the gateway origin it is presented to -- into the same
    ``~/.onex/config.yaml`` under two different top-level names. ``onex auth
    login --api-key-stdin`` wrote ``gateway:`` (OMN-17205); ``onex cloud login``
    wrote ``cloud:`` (OMN-16967). Each side hand-rolled its own dict access over
    the shared file, so neither could observe the other, and a machine onboarded
    through one command was told by the other that it held no key at all.

    ``gateway:`` is now the single home. It already models both credential kinds
    a machine may hold and already refuses a machine carrying both rather than
    resolving one by precedence, so it is the block that survives.

WHY IT IS ONE-SHOT AND NOT A COMPATIBILITY LAYER
    A reader that accepts both names forever is the defect with a longer name:
    it leaves two live homes on disk and defers the choice to whichever caller
    ran last. This runs once per machine, rewrites the block, and REMOVES the
    retired one, so the second call has nothing to do and says so. The retired
    name appears in this module and nowhere else in the package; a test asserts
    that, with a positive control proving the sweep can find it here.

WHAT IT DELIBERATELY DOES NOT TOUCH
    ``credentials.json``. The migration carries the existing ``api_key_ref``
    across unchanged, so the key value is never re-read, never re-written, and
    never passes through this process. A migration that rewrote the secret file
    would put a live credential in the one place it had no reason to be.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import yaml
from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode
from omnibase_core.errors.model_onex_error import ModelOnexError
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "LEGACY_CLOUD_BLOCK",
    "ModelLegacyCloudBlockMigration",
    "migrate_legacy_cloud_block",
]

#: The retired top-level block name. Referenced here and nowhere else.
LEGACY_CLOUD_BLOCK: Final[str] = "cloud"
CANONICAL_BLOCK: Final[str] = "gateway"

_KEY_REF: Final[str] = "api_key_ref"  # pragma: allowlist secret
_BASE_URL: Final[str] = "base_url"
#: The retired block's operator label. It named which key this was, which is
#: what ``tenant_slug`` names on the canonical block -- except that the
#: canonical one is verified against the gateway rather than free text.
_LEGACY_LABEL: Final[str] = "profile"


class ModelLegacyCloudBlockMigration(BaseModel):
    """What the migration did, so a caller can report it without re-reading disk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    migrated: bool
    #: Why nothing happened, when nothing happened. Empty on a real migration.
    reason: str = Field(default="")
    #: The tenant the migrated block now names. ``None`` when nothing moved.
    tenant_slug: str | None = Field(default=None)


def _load_document(config_path: Path) -> dict[str, object] | None:
    if not config_path.exists():
        return None
    # yaml-ok: user-authored config with several independent writers (OMN-16037);
    # a model here would drop another writer's top-level keys on the round trip.
    document = yaml.safe_load(config_path.read_text())
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise ModelOnexError(
            f"{config_path} must be a YAML mapping, found {type(document).__name__}.",
            error_code=EnumCoreErrorCode.CONFIGURATION_PARSE_ERROR,
        )
    return {str(key): value for key, value in document.items()}


def _require_text(block: dict[str, object], key: str, config_path: Path) -> str:
    value = block.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ModelOnexError(
            f"{config_path}: the retired '{LEGACY_CLOUD_BLOCK}' block cannot be "
            f"migrated because '{LEGACY_CLOUD_BLOCK}.{key}' is missing or blank. "
            "Delete the block and re-run 'onex cloud login'.",
            error_code=EnumCoreErrorCode.INVALID_CONFIGURATION,
        )
    return value


def migrate_legacy_cloud_block(*, onex_home: Path) -> ModelLegacyCloudBlockMigration:
    """Move a retired ``cloud:`` block onto the canonical block, exactly once.

    Args:
        onex_home: Directory holding ``config.yaml``. Injected rather than
            derived from ``Path.home()`` so tests drive a real directory.

    Returns:
        What happened, typed. ``migrated`` is False with a ``reason`` whenever
        there was nothing to move.

    Raises:
        ModelOnexError: When the file is unparseable, when the retired block is
            malformed, or when BOTH blocks name a key. The last case is refused
            rather than resolved by precedence: two homes for one credential is
            the defect this migration closes, and silently preferring either one
            leaves the loser's key live on disk and unread.
    """
    config_path = onex_home / "config.yaml"
    document = _load_document(config_path)
    if document is None:
        return ModelLegacyCloudBlockMigration(migrated=False, reason="no_config")

    if LEGACY_CLOUD_BLOCK not in document:
        return ModelLegacyCloudBlockMigration(migrated=False, reason="no_legacy_block")

    legacy = document[LEGACY_CLOUD_BLOCK]
    if not isinstance(legacy, dict):
        raise ModelOnexError(
            f"{config_path}: '{LEGACY_CLOUD_BLOCK}' must be a mapping, found "
            f"{type(legacy).__name__}.",
            error_code=EnumCoreErrorCode.CONFIGURATION_PARSE_ERROR,
        )
    legacy_block = {str(key): value for key, value in legacy.items()}

    canonical = document.get(CANONICAL_BLOCK)
    if isinstance(canonical, dict) and _KEY_REF in canonical:
        raise ModelOnexError(
            f"{config_path} names an API key in BOTH the retired "
            f"'{LEGACY_CLOUD_BLOCK}' block and the canonical "
            f"'{CANONICAL_BLOCK}' block. Refusing to choose one for you: the "
            "one not chosen would stay live on disk and unread, which is the "
            f"defect this migration closes. Delete the '{LEGACY_CLOUD_BLOCK}' "
            "block if the gateway block is the key you meant, or delete the "
            f"'{CANONICAL_BLOCK}' block and re-run the migration if it is not.",
            error_code=EnumCoreErrorCode.INVALID_CONFIGURATION,
        )

    tenant_slug = _require_text(legacy_block, _LEGACY_LABEL, config_path)
    migrated_block: dict[str, object] = {
        "tenant_slug": tenant_slug,
        _KEY_REF: _require_text(legacy_block, _KEY_REF, config_path),
        _BASE_URL: _require_text(legacy_block, _BASE_URL, config_path),
    }

    # A canonical block holding a client secret and no API key is left alone
    # rather than overwritten: that is a different credential kind, and the
    # canonical store refuses a block naming both. Replacing it here would
    # destroy a credential the operator never asked us to touch.
    if isinstance(canonical, dict) and canonical:
        raise ModelOnexError(
            f"{config_path}: the canonical '{CANONICAL_BLOCK}' block already "
            "holds a credential of another kind. Migrating the retired "
            f"'{LEGACY_CLOUD_BLOCK}' block over it would destroy that "
            "credential. Remove whichever block you did not mean, then re-run.",
            error_code=EnumCoreErrorCode.INVALID_CONFIGURATION,
        )

    document[CANONICAL_BLOCK] = migrated_block
    del document[LEGACY_CLOUD_BLOCK]
    config_path.write_text(yaml.safe_dump(document, sort_keys=False))

    return ModelLegacyCloudBlockMigration(migrated=True, tenant_slug=tenant_slug)
