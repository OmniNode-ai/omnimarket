# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the GitHub App installation-token minting path (OMN-14893).

Pins the contract:

* :func:`assert_is_app_installation_token` accepts ONLY ``ghs_``-prefixed
  tokens and rejects every human-PAT shape (``ghp_``, ``gho_``,
  ``github_pat_``) — the identity guard (#4).
* :func:`mint_installation_token` signs a decodable RS256 JWT, resolves the
  installation, exchanges for a token, and re-runs the identity guard on the
  minted token before returning it.
* :func:`resolve_app_installation_token_from_contract` fails loud (naming
  the exact missing secret ref) when either credential is unresolvable, and
  NEVER falls back to ``GITHUB_TOKEN`` — there is no PAT-reading code in this
  function at all.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import jwt as pyjwt
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from omnimarket.github_api import GitHubApiError
from omnimarket.github_app_auth import (
    GitHubAppCredentialMissingError,
    GitHubAppIdentityError,
    assert_is_app_installation_token,
    mint_installation_token,
    resolve_app_installation_token_from_contract,
)

_MOD = "omnimarket.github_app_auth"


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[str, str]:
    """Return (private_pem, public_pem) for a throwaway 2048-bit test key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return private_pem, public_pem


# ---------------------------------------------------------------------------
# assert_is_app_installation_token — the identity guard (#4)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAssertIsAppInstallationToken:
    def test_accepts_ghs_prefixed_token(self) -> None:
        assert_is_app_installation_token("ghs_realInstallationToken123")  # no raise

    @pytest.mark.parametrize(
        "human_pat",
        [
            "ghp_classicPersonalAccessToken",
            "gho_oauthToken",
            "github_pat_fineGrainedToken",
            "",
            "not-a-github-token-at-all",
        ],
    )
    def test_rejects_every_human_pat_shape(self, human_pat: str) -> None:
        with pytest.raises(GitHubAppIdentityError, match="NOT a"):
            assert_is_app_installation_token(human_pat)


# ---------------------------------------------------------------------------
# mint_installation_token
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMintInstallationToken:
    def test_signs_decodable_rs256_jwt_and_returns_app_token(
        self, rsa_keypair: tuple[str, str]
    ) -> None:
        private_pem, public_pem = rsa_keypair
        captured_jwts: list[str] = []

        def fake_rest(method: str, path: str, *, token: str, body=None):
            captured_jwts.append(token)
            if path == "/orgs/OmniNode-ai/installation":
                assert method == "GET"
                return {"id": 148180820}
            if path == "/app/installations/148180820/access_tokens":
                assert method == "POST"
                assert body == {"repositories": ["onex_change_control"]}
                return {"token": "ghs_mintedInstallationToken"}
            raise AssertionError(f"unexpected call: {method} {path}")

        with patch(f"{_MOD}.rest_json", side_effect=fake_rest):
            token = mint_installation_token(
                app_id="148180820",
                private_key_pem=private_pem,
                repositories=["onex_change_control"],
            )

        assert token == "ghs_mintedInstallationToken"
        # Both calls (installation resolve + token exchange) authenticated
        # with the SAME signed JWT, decodable with the matching public key.
        assert len(captured_jwts) == 2
        assert captured_jwts[0] == captured_jwts[1]
        decoded = pyjwt.decode(
            captured_jwts[0],
            public_pem,
            algorithms=["RS256"],
            options={"verify_exp": False},
        )
        assert decoded["iss"] == "148180820"

    def test_rejects_non_app_token_from_exchange(
        self, rsa_keypair: tuple[str, str]
    ) -> None:
        """A mint that somehow returns a PAT-shaped token must still fail loud."""
        private_pem, _ = rsa_keypair

        def fake_rest(method: str, path: str, *, token: str, body=None):
            if "installation" in path and "access_tokens" not in path:
                return {"id": 1}
            return {"token": "ghp_thisShouldNeverHappen"}

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            pytest.raises(GitHubAppIdentityError, match="NOT a"),
        ):
            mint_installation_token(app_id="1", private_key_pem=private_pem)

    def test_raises_when_exchange_returns_no_token(
        self, rsa_keypair: tuple[str, str]
    ) -> None:
        private_pem, _ = rsa_keypair

        def fake_rest(method: str, path: str, *, token: str, body=None):
            if "installation" in path and "access_tokens" not in path:
                return {"id": 1}
            return {}

        with (
            patch(f"{_MOD}.rest_json", side_effect=fake_rest),
            pytest.raises(GitHubAppIdentityError, match="no token"),
        ):
            mint_installation_token(app_id="1", private_key_pem=private_pem)

    def test_raises_when_installation_id_unresolvable(
        self, rsa_keypair: tuple[str, str]
    ) -> None:
        private_pem, _ = rsa_keypair

        with (
            patch(f"{_MOD}.rest_json", return_value={"id": "not-an-int"}),
            pytest.raises(GitHubAppIdentityError, match="installation id"),
        ):
            mint_installation_token(app_id="1", private_key_pem=private_pem)


# ---------------------------------------------------------------------------
# resolve_app_installation_token_from_contract — fail-loud, no PAT fallback
# ---------------------------------------------------------------------------

_CONTRACT_TEXT = """
name: fake_node
secrets:
  GITHUB_TOKEN:
    required: true
  ONEXBOT_OCC_APP_ID:
    required: false
  ONEXBOT_OCC_PRIVATE_KEY:
    required: false
"""


@pytest.mark.unit
class TestResolveAppInstallationTokenFromContract:
    def test_missing_app_id_raises_naming_the_ref_never_touches_pat(
        self, tmp_path: Path
    ) -> None:
        contract_path = tmp_path / "contract.yaml"
        contract_path.write_text(_CONTRACT_TEXT, encoding="utf-8")

        with (
            patch(f"{_MOD}.resolve_api_key", return_value=None) as mock_resolve,
            patch(f"{_MOD}.mint_installation_token") as mock_mint,
            pytest.raises(GitHubAppCredentialMissingError, match="ONEXBOT_OCC_APP_ID"),
        ):
            resolve_app_installation_token_from_contract(contract_path)

        # Never reached the mint call, and never asked the resolver for
        # GITHUB_TOKEN — this function has no code path to the PAT at all.
        mock_mint.assert_not_called()
        for call in mock_resolve.call_args_list:
            assert call.args[0] != "GITHUB_TOKEN"

    def test_missing_private_key_raises_naming_the_ref(self, tmp_path: Path) -> None:
        contract_path = tmp_path / "contract.yaml"
        contract_path.write_text(_CONTRACT_TEXT, encoding="utf-8")

        def fake_resolve(ref: str, **_kwargs: object) -> SecretStr | None:
            if ref == "ONEXBOT_OCC_APP_ID":
                return SecretStr("148180820")
            return None

        with (
            patch(f"{_MOD}.resolve_api_key", side_effect=fake_resolve),
            patch(f"{_MOD}.mint_installation_token") as mock_mint,
            pytest.raises(
                GitHubAppCredentialMissingError, match="ONEXBOT_OCC_PRIVATE_KEY"
            ),
        ):
            resolve_app_installation_token_from_contract(contract_path)

        mock_mint.assert_not_called()

    def test_success_mints_with_resolved_credentials(self, tmp_path: Path) -> None:
        contract_path = tmp_path / "contract.yaml"
        contract_path.write_text(_CONTRACT_TEXT, encoding="utf-8")

        def fake_resolve(ref: str, **_kwargs: object) -> SecretStr | None:
            if ref == "ONEXBOT_OCC_APP_ID":
                return SecretStr("148180820")
            if ref == "ONEXBOT_OCC_PRIVATE_KEY":
                return SecretStr(
                    "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----"
                )
            return None

        with (
            patch(f"{_MOD}.resolve_api_key", side_effect=fake_resolve),
            patch(
                f"{_MOD}.mint_installation_token", return_value="ghs_ok"
            ) as mock_mint,
        ):
            token = resolve_app_installation_token_from_contract(contract_path)

        assert token == "ghs_ok"
        mock_mint.assert_called_once()
        assert mock_mint.call_args.kwargs["app_id"] == "148180820"

    def test_wraps_github_api_error_from_mint(self, tmp_path: Path) -> None:
        contract_path = tmp_path / "contract.yaml"
        contract_path.write_text(_CONTRACT_TEXT, encoding="utf-8")

        def fake_resolve(ref: str, **_kwargs: object) -> SecretStr | None:
            return SecretStr("value")

        with (
            patch(f"{_MOD}.resolve_api_key", side_effect=fake_resolve),
            patch(
                f"{_MOD}.mint_installation_token",
                side_effect=GitHubApiError("boom", status_code=500),
            ),
            pytest.raises(GitHubAppIdentityError, match="mint failed"),
        ):
            resolve_app_installation_token_from_contract(contract_path)

    def test_contract_missing_secret_declaration_raises(self, tmp_path: Path) -> None:
        """A contract that never declares the secret fails via contract_secret_ref."""
        contract_path = tmp_path / "contract.yaml"
        contract_path.write_text("name: fake_node\nsecrets: {}\n", encoding="utf-8")

        with pytest.raises(ValueError, match="ONEXBOT_OCC_APP_ID"):
            resolve_app_installation_token_from_contract(contract_path)


def test_module_contract_is_yaml_parseable() -> None:
    """Sanity: the fixture contract text used above is valid YAML (self-check)."""
    parsed = yaml.safe_load(_CONTRACT_TEXT)
    assert "ONEXBOT_OCC_APP_ID" in parsed["secrets"]
