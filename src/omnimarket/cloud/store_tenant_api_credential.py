# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""StoreTenantApiCredential — the ``~/.onex`` dashboard API key (OMN-16967).

Same two-file split, same rules, and deliberately the same reader/writer shape
as :mod:`~omnibase_infra.gateway.client.store_gateway_credential`: a customer's
credential must not be stored by a second set of conventions invented for it.

``~/.onex/config.yaml`` carries a ``cloud:`` block of references and the gateway
origin, never a key VALUE. A literal ``api_key`` there is refused outright
rather than accepted-with-a-warning — config.yaml is world-readable by default
and is the file people paste into support threads.

``~/.onex/credentials.json`` (mode 0600, enforced on read as well as write)
holds ``{<ref>: <key>}``, shared with the gateway store: one secret file per
machine, two blocks referencing into it.

``base_url`` has NO default anywhere in this module. An unset origin is a
refusal naming ``onex cloud login`` — never a substituted host. A default here
would send a live customer key to whatever origin the code shipped with, which
is the defect class this ticket's lane was told to avoid.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Final

import yaml
from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode
from omnibase_core.errors.model_onex_error import ModelOnexError
from pydantic import SecretStr

from omnimarket.cloud.model_tenant_api_credential import (
    ModelTenantApiCredential,
)

__all__ = ["StoreTenantApiCredential"]

_CLOUD_BLOCK: Final[str] = "cloud"
_KEY_REF_KEY: Final[str] = "api_key_ref"
_REMEDIATION: Final[str] = (
    "run 'onex cloud login --base-url <gateway origin> --api-key-stdin' and "
    "paste the onxk_ key you created in the dashboard"
)
_REQUIRED_KEYS: Final[tuple[str, ...]] = ("base_url", _KEY_REF_KEY, "profile")


class StoreTenantApiCredential:
    """Reads and writes the tenant API credential under an ``~/.onex`` root."""

    def __init__(self, *, onex_home: Path) -> None:
        """Bind the store to a directory.

        Args:
            onex_home: Directory holding ``config.yaml`` and
                ``credentials.json``. Injected rather than derived from
                ``Path.home()`` so tests drive a real directory instead of
                patching the home lookup.
        """
        self._onex_home = onex_home

    @property
    def config_path(self) -> Path:
        return self._onex_home / "config.yaml"

    @property
    def credentials_path(self) -> Path:
        return self._onex_home / "credentials.json"

    # -- read --------------------------------------------------------------

    def load(self) -> ModelTenantApiCredential:
        """Resolve the credential, or raise naming what to do about it.

        Raises:
            ModelOnexError: On any missing, blank, malformed, mis-permissioned
                or key-carrying configuration. Never returns a partially
                resolved credential — a half-configured customer credential is
                how an anonymous call gets mistaken for an authenticated one.
        """
        block = self._load_cloud_block()

        if "api_key" in block:
            raise ModelOnexError(
                f"{self.config_path} carries an inline 'api_key'. The key value "
                f"must live only in {self.credentials_path} (mode 0600), "
                f"referenced from config by '{_KEY_REF_KEY}'. Remove it, then "
                f"{_REMEDIATION}.",
                error_code=EnumCoreErrorCode.INVALID_CONFIGURATION,
            )

        values = {key: self._require_text(block, key) for key in _REQUIRED_KEYS}
        api_key = self._read_secret(values[_KEY_REF_KEY])

        return ModelTenantApiCredential(
            base_url=values["base_url"],
            api_key=SecretStr(api_key),
            profile=values["profile"],
        )

    def _load_cloud_block(self) -> dict[str, object]:
        document = self._load_config_document(must_exist=True)
        block = document.get(_CLOUD_BLOCK)
        if block is None:
            raise ModelOnexError(
                f"{self.config_path} has no '{_CLOUD_BLOCK}:' block — this "
                f"machine holds no OmniNode API key. To create one, {_REMEDIATION}.",
                error_code=EnumCoreErrorCode.CONFIGURATION_NOT_FOUND,
            )
        if not isinstance(block, dict):
            raise ModelOnexError(
                f"{self.config_path}: '{_CLOUD_BLOCK}' must be a mapping, "
                f"found {type(block).__name__}.",
                error_code=EnumCoreErrorCode.CONFIGURATION_PARSE_ERROR,
            )
        return {str(key): value for key, value in block.items()}

    def _load_config_document(self, *, must_exist: bool) -> dict[str, object]:
        if not self.config_path.exists():
            if not must_exist:
                return {}
            raise ModelOnexError(
                f"no ONEX config at {self.config_path} — this machine holds no "
                f"OmniNode API key. To create one, {_REMEDIATION}.",
                error_code=EnumCoreErrorCode.CONFIGURATION_NOT_FOUND,
            )
        # yaml-ok: user-authored config file with several independent writers
        # (OMN-16037); a Pydantic model here would either reject another
        # writer's keys or silently drop them on the round trip.
        document = yaml.safe_load(self.config_path.read_text())
        if document is None:
            return {}
        if not isinstance(document, dict):
            raise ModelOnexError(
                f"{self.config_path} must be a YAML mapping, found "
                f"{type(document).__name__}.",
                error_code=EnumCoreErrorCode.CONFIGURATION_PARSE_ERROR,
            )
        return {str(key): value for key, value in document.items()}

    def _require_text(self, block: dict[str, object], key: str) -> str:
        """Read one non-blank string, treating blank as absent-and-wrong."""
        if key not in block:
            raise ModelOnexError(
                f"{self.config_path}: '{_CLOUD_BLOCK}.{key}' is missing. To "
                f"rewrite the block, {_REMEDIATION}.",
                error_code=EnumCoreErrorCode.MISSING_REQUIRED_PARAMETER,
            )
        value = block[key]
        if not isinstance(value, str) or not value.strip():
            raise ModelOnexError(
                f"{self.config_path}: '{_CLOUD_BLOCK}.{key}' must be a "
                f"non-empty string. To rewrite the block, {_REMEDIATION}.",
                error_code=EnumCoreErrorCode.INVALID_CONFIGURATION,
            )
        return value

    def _read_secret(self, key_ref: str) -> str:
        """Resolve the referenced key from the 0600 credentials file."""
        if not self.credentials_path.exists():
            raise ModelOnexError(
                f"no credentials.json at {self.credentials_path}, but config "
                f"references key '{key_ref}'. To restore it, {_REMEDIATION}.",
                error_code=EnumCoreErrorCode.CONFIGURATION_NOT_FOUND,
            )

        mode = stat.S_IMODE(self.credentials_path.stat().st_mode)
        if mode & 0o077:
            raise ModelOnexError(
                f"{self.credentials_path} is mode {mode:04o}; it must be 0600 "
                "(owner-only). Refusing to read a group- or world-readable "
                f"credential file. Fix with: chmod 600 {self.credentials_path}",
                error_code=EnumCoreErrorCode.PERMISSION_DENIED,
            )

        try:
            document = json.loads(self.credentials_path.read_text())
        except json.JSONDecodeError as exc:
            raise ModelOnexError(
                f"{self.credentials_path} is not valid JSON. To rewrite it, "
                f"{_REMEDIATION}.",
                error_code=EnumCoreErrorCode.CONFIGURATION_PARSE_ERROR,
            ) from exc

        if not isinstance(document, dict):
            raise ModelOnexError(
                f"{self.credentials_path} must be a JSON object mapping "
                "credential refs to values.",
                error_code=EnumCoreErrorCode.CONFIGURATION_PARSE_ERROR,
            )
        if key_ref not in document:
            raise ModelOnexError(
                f"{self.credentials_path} has no entry for key ref "
                f"'{key_ref}' named by {self.config_path}. To restore it, "
                f"{_REMEDIATION}.",
                error_code=EnumCoreErrorCode.CONFIGURATION_NOT_FOUND,
            )
        api_key = document[key_ref]
        if not isinstance(api_key, str) or not api_key:
            raise ModelOnexError(
                f"{self.credentials_path}: entry '{key_ref}' must be a "
                "non-empty string.",
                error_code=EnumCoreErrorCode.INVALID_CONFIGURATION,
            )
        return api_key

    # -- write -------------------------------------------------------------

    def save(self, *, base_url: str, api_key: str, profile: str) -> None:
        """Write the reference-only config block and the 0600 key file.

        Every other top-level key in ``config.yaml`` survives the round trip --
        ``onex cloud login`` must not be a way to lose someone's ``gateway:``
        or ``kafka:`` settings.
        """
        key_ref = f"{profile}-cloud-api-key"
        self._onex_home.mkdir(parents=True, exist_ok=True)

        document = self._load_config_document(must_exist=False)
        document[_CLOUD_BLOCK] = {
            "base_url": base_url,
            _KEY_REF_KEY: key_ref,
            "profile": profile,
        }
        self.config_path.write_text(yaml.safe_dump(document, sort_keys=False))

        secrets = self._load_secret_document()
        secrets[key_ref] = api_key
        self._write_secret_document(secrets)

    def clear(self) -> None:
        """Remove both the config block and the referenced key.

        Order matters: the key goes first. If the process dies between the two
        writes, what survives is a config naming a missing key — which ``load``
        refuses loudly — rather than an orphaned credential on disk with
        nothing pointing at it.
        """
        document = self._load_config_document(must_exist=False)
        block = document.get(_CLOUD_BLOCK)
        key_ref = ""
        if isinstance(block, dict):
            candidate = block.get(_KEY_REF_KEY)
            if isinstance(candidate, str):
                key_ref = candidate

        if key_ref:
            secrets = self._load_secret_document()
            if key_ref in secrets:
                del secrets[key_ref]
                self._write_secret_document(secrets)

        if _CLOUD_BLOCK in document:
            del document[_CLOUD_BLOCK]
            self.config_path.write_text(yaml.safe_dump(document, sort_keys=False))

    def _load_secret_document(self) -> dict[str, str]:
        if not self.credentials_path.exists():
            return {}
        try:
            document = json.loads(self.credentials_path.read_text())
        except json.JSONDecodeError as exc:
            raise ModelOnexError(
                f"{self.credentials_path} is not valid JSON; refusing to "
                "overwrite it and lose the credentials it may hold.",
                error_code=EnumCoreErrorCode.CONFIGURATION_PARSE_ERROR,
            ) from exc
        if not isinstance(document, dict):
            raise ModelOnexError(
                f"{self.credentials_path} must be a JSON object.",
                error_code=EnumCoreErrorCode.CONFIGURATION_PARSE_ERROR,
            )
        return {str(key): str(value) for key, value in document.items()}

    def _write_secret_document(self, secrets: dict[str, str]) -> None:
        """Write the key file so it is never briefly world-readable.

        ``touch`` + ``chmod`` before ``write_text``: creating the file at the
        umask default and tightening it afterwards leaves a window in which the
        credential is on disk at 0644.
        """
        self.credentials_path.touch(mode=0o600, exist_ok=True)
        self.credentials_path.chmod(0o600)
        self.credentials_path.write_text(json.dumps(secrets, indent=2, sort_keys=True))
