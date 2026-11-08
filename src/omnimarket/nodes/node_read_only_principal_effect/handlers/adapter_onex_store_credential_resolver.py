# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real credential resolver: the machine's onex credential store, read through its own loader."""

from __future__ import annotations

from pathlib import Path

from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_infra.cli.store_lane_credential import StoreLaneCredential

from ..models.model_read_only_principal import EnumCredentialStore, ModelCredentialRef


class OnexStoreCredentialResolver:
    """Resolve a reference by loading the lane's identity from the store under the running home.

    The store's loader refuses a missing, blank, malformed, mis-permissioned or value-carrying entry, and
    that refusal is the answer ``False``. The loaded value is dropped at once and never leaves this method.
    """

    def __init__(self, onex_home: Path | None = None) -> None:
        self._home = onex_home if onex_home is not None else Path.home() / ".onex"

    def resolves(self, reference: ModelCredentialRef) -> bool:
        if reference.store != EnumCredentialStore.ONEX_CREDENTIAL_STORE:
            return False
        store = StoreLaneCredential(onex_home=self._home)
        try:
            loaded = store.load(reference.lane)
        except ModelOnexError:
            return False
        return loaded.sasl_password_ref == reference.secret_ref
