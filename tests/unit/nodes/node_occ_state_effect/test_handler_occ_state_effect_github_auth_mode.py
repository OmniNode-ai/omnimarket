# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The mint's READ half honours the same auth-mode seam as its write half (OMN-18439).

``node_occ_companion_effect`` has authenticated as the ``onexbot-occ-writer``
App since OMN-14893, and the ``.201`` effects lane runs it that way. The read
half it drives in-process, ``node_occ_state_effect``, had no auth-mode branch
at all: it resolved the contract-declared ``GITHUB_TOKEN`` unconditionally.
Because ``_mint_once`` calls the state handler as the FIRST GitHub I/O of every
mint, every companion spent roughly eight REST calls of the shared operator
PAT's budget before the App credential was ever touched -- measured live on the
dev lane as 16 of the newest 30 records on
``onex.evt.omnimarket.occ-companion-effect-failed.v1`` carrying GitHub's own
``API rate limit exceeded for user ID 1002253``.

These tests pin the seam from the read half's side. ``pat`` mode must stay
byte-for-byte unchanged, because the CI mutate path deliberately exports a
NARROWED, ``onex_change_control``-scoped App token as ``GITHUB_TOKEN`` and
relies on that narrowing (OMN-15441); ``app`` mode must reach the installation
token and must not be able to reach the PAT resolver at all.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import SecretStr

from omnimarket.github_app_auth import GitHubAppCredentialMissingError
from omnimarket.nodes.node_occ_state_effect.handlers.handler_occ_state_effect import (
    _resolve_github_token,
)

_SEAM = "omnimarket.occ_github_auth"


@pytest.mark.unit
class TestOccStateEffectAuthMode:
    def test_pat_mode_resolves_the_contract_declared_token_unchanged(
        self, monkeypatch
    ) -> None:
        """AC7 second half: the CI-narrowed token keeps working.

        ``call-occ-companion-author.yml`` exports an ``onex_change_control``-scoped
        App token as ``GITHUB_TOKEN`` and runs the node in pat mode. Broadening
        that to an org-wide mint would be a privilege broadening, so pat mode is
        pinned here rather than left to drift.
        """
        monkeypatch.delenv("OMNI_OCC_GITHUB_AUTH_MODE", raising=False)
        with (
            patch(f"{_SEAM}.contract_secret_ref", return_value="GITHUB_TOKEN"),
            patch(f"{_SEAM}.resolve_api_key", return_value=SecretStr("ghs_narrowed")),
        ):
            assert _resolve_github_token() == "ghs_narrowed"

    def test_app_mode_reaches_the_installation_token_and_never_the_pat(
        self, monkeypatch
    ) -> None:
        """AC1 + AC7 first half.

        The PAT resolver is poisoned to raise, so a handler that still reaches
        it fails loudly here rather than passing on a stubbed value.
        """
        monkeypatch.setenv("OMNI_OCC_GITHUB_AUTH_MODE", "app")
        with (
            patch(
                f"{_SEAM}.resolve_app_installation_token_from_contract",
                return_value="ghs_appminted",
            ) as mock_app,
            patch(
                f"{_SEAM}.resolve_api_key",
                side_effect=AssertionError(
                    "the read half reached GITHUB_TOKEN in app-auth mode"
                ),
            ) as mock_pat,
        ):
            assert _resolve_github_token() == "ghs_appminted"
        mock_app.assert_called_once()
        mock_pat.assert_not_called()

    def test_app_mode_passes_this_nodes_own_contract_to_the_mint(
        self, monkeypatch
    ) -> None:
        """The App credential refs are read from THIS node's contract.

        ``resolve_app_installation_token_from_contract`` reads the declared
        secret names out of the contract it is handed, so handing it the
        companion effect's contract instead would make this node's own
        ``secrets:`` block decorative.
        """
        monkeypatch.setenv("OMNI_OCC_GITHUB_AUTH_MODE", "app")
        with patch(
            f"{_SEAM}.resolve_app_installation_token_from_contract",
            return_value="ghs_appminted",
        ) as mock_app:
            _resolve_github_token()
        contract_path = mock_app.call_args.args[0]
        assert contract_path.parent.name == "node_occ_state_effect"
        assert contract_path.name == "contract.yaml"

    def test_app_mode_credential_missing_propagates_with_no_pat_fallback(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("OMNI_OCC_GITHUB_AUTH_MODE", "app")
        with (
            patch(
                f"{_SEAM}.resolve_app_installation_token_from_contract",
                side_effect=GitHubAppCredentialMissingError(
                    "ONEXBOT_OCC_PRIVATE_KEY missing"
                ),
            ),
            patch(f"{_SEAM}.resolve_api_key") as mock_pat,
            pytest.raises(
                GitHubAppCredentialMissingError, match="ONEXBOT_OCC_PRIVATE_KEY"
            ),
        ):
            _resolve_github_token()
        mock_pat.assert_not_called()

    def test_unknown_mode_fails_closed(self, monkeypatch) -> None:
        monkeypatch.setenv("OMNI_OCC_GITHUB_AUTH_MODE", "BOGUS")
        with pytest.raises(RuntimeError, match="not a recognized OCC"):
            _resolve_github_token()
