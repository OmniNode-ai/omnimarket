# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``_resolve_github_token`` auth-mode switch (OMN-14893).

Pins the same contract as the sibling ``OccCompanionEmitter`` producer:

* default (``pat``) mode is byte-for-byte unchanged — the contract-declared
  ``GITHUB_TOKEN`` ref resolves as before;
* ``OMNI_OCC_GITHUB_AUTH_MODE=app`` routes through
  ``resolve_app_installation_token_from_contract`` and NEVER touches the PAT
  resolver at all;
* a credential-missing error in app mode propagates as-is (no swallow, no
  silent PAT fallback);
* an unrecognized mode value fails loud.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import SecretStr

from omnimarket.github_app_auth import GitHubAppCredentialMissingError
from omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect import (
    _resolve_github_token,
)

_MOD = (
    "omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect"
)


@pytest.mark.unit
class TestResolveGithubTokenAuthMode:
    def test_default_mode_is_pat_unchanged_behavior(self, monkeypatch) -> None:
        monkeypatch.delenv("OMNI_OCC_GITHUB_AUTH_MODE", raising=False)
        with (
            patch(f"{_MOD}.contract_secret_ref", return_value="GITHUB_TOKEN"),
            patch(f"{_MOD}.resolve_api_key", return_value=SecretStr("ghp_humanpat")),
        ):
            token = _resolve_github_token()
        assert token == "ghp_humanpat"

    def test_app_mode_routes_through_app_auth_never_touches_pat(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("OMNI_OCC_GITHUB_AUTH_MODE", "app")
        with (
            patch(
                f"{_MOD}.resolve_app_installation_token_from_contract",
                return_value="ghs_appminted",
            ) as mock_app_resolve,
            patch(f"{_MOD}.resolve_api_key") as mock_pat_resolve,
        ):
            token = _resolve_github_token()
        assert token == "ghs_appminted"
        mock_app_resolve.assert_called_once()
        mock_pat_resolve.assert_not_called()

    def test_unknown_mode_raises(self, monkeypatch) -> None:
        monkeypatch.setenv("OMNI_OCC_GITHUB_AUTH_MODE", "BOGUS")
        with pytest.raises(RuntimeError, match="not a recognized OCC"):
            _resolve_github_token()

    def test_app_mode_credential_missing_propagates_no_pat_fallback(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("OMNI_OCC_GITHUB_AUTH_MODE", "app")
        with (
            patch(
                f"{_MOD}.resolve_app_installation_token_from_contract",
                side_effect=GitHubAppCredentialMissingError(
                    "ONEXBOT_OCC_PRIVATE_KEY missing"
                ),
            ),
            patch(f"{_MOD}.resolve_api_key") as mock_pat_resolve,
            pytest.raises(
                GitHubAppCredentialMissingError, match="ONEXBOT_OCC_PRIVATE_KEY"
            ),
        ):
            _resolve_github_token()
        mock_pat_resolve.assert_not_called()
