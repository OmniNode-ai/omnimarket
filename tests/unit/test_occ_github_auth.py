# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The single OCC GitHub-auth seam (OMN-18439).

The mode switch existed three times before this module: in
``occ_companion_emitter``, in ``handler_occ_companion_effect``, and nowhere at
all in ``handler_occ_state_effect``. Two of the three agreed and the third --
the read half that runs FIRST in every mint -- silently resolved the shared
operator PAT, which is how a lane configured for app-auth still exhausted a
human's rate-limit budget.

These tests pin the seam itself: the mode parse, the two branches, and the
structural property that there is exactly one definition for all three callers.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import SecretStr

import omnimarket.occ_github_auth as seam
from omnimarket.github_app_auth import GitHubAppCredentialMissingError
from omnimarket.occ_github_auth import (
    GITHUB_AUTH_MODE_ENV_VAR,
    EnumOccGitHubAuthMode,
    resolve_occ_auth_mode,
    resolve_occ_github_token,
)

_CONTRACT = Path("/nonexistent/contract.yaml")

#: Every OCC node that resolves a GitHub credential through the seam.
_SEAM_CALLERS = (
    "omnimarket.nodes.node_occ_state_effect.handlers.handler_occ_state_effect",
    "omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect",
    "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter",
)


@pytest.mark.unit
class TestResolveOccAuthMode:
    def test_unset_defaults_to_pat(self, monkeypatch) -> None:
        monkeypatch.delenv(GITHUB_AUTH_MODE_ENV_VAR, raising=False)
        assert resolve_occ_auth_mode() is EnumOccGitHubAuthMode.PAT

    def test_empty_and_whitespace_default_to_pat(self, monkeypatch) -> None:
        """An env var set to the empty string is how compose passes "unset".

        ``docker-compose.infra.yml`` forwards this variable as
        ``${OMNI_OCC_GITHUB_AUTH_MODE:-}``, so a lane that has not set it gets
        an EMPTY value rather than an absent one. Treating that as a bad mode
        would fail every node on every lane that never opted in.
        """
        for raw in ("", "   "):
            monkeypatch.setenv(GITHUB_AUTH_MODE_ENV_VAR, raw)
            assert resolve_occ_auth_mode() is EnumOccGitHubAuthMode.PAT

    def test_case_and_padding_are_normalized(self, monkeypatch) -> None:
        monkeypatch.setenv(GITHUB_AUTH_MODE_ENV_VAR, "  APP  ")
        assert resolve_occ_auth_mode() is EnumOccGitHubAuthMode.APP

    def test_unrecognized_mode_fails_closed_naming_the_alternatives(
        self, monkeypatch
    ) -> None:
        """A typo must raise, never degrade to pat.

        A silent degrade would still mint a working companion -- on the wrong
        identity, with no signal. That is precisely the invisible
        mis-attribution OMN-14893 closed.
        """
        monkeypatch.setenv(GITHUB_AUTH_MODE_ENV_VAR, "ap")
        with pytest.raises(RuntimeError, match="not a recognized OCC") as excinfo:
            resolve_occ_auth_mode()
        assert "'pat'" in str(excinfo.value)
        assert "'app'" in str(excinfo.value)


@pytest.mark.unit
class TestResolveOccGithubToken:
    def test_pat_mode_reads_the_contract_declared_ref(self, monkeypatch) -> None:
        monkeypatch.delenv(GITHUB_AUTH_MODE_ENV_VAR, raising=False)
        with (
            patch.object(seam, "contract_secret_ref", return_value="GITHUB_TOKEN") as m,
            patch.object(seam, "resolve_api_key", return_value=SecretStr("ghp_pat")),
        ):
            assert resolve_occ_github_token(_CONTRACT) == "ghp_pat"
        m.assert_called_once_with(_CONTRACT, "GITHUB_TOKEN")

    def test_pat_mode_unresolvable_ref_raises(self, monkeypatch) -> None:
        monkeypatch.delenv(GITHUB_AUTH_MODE_ENV_VAR, raising=False)
        with (
            patch.object(seam, "contract_secret_ref", return_value="GITHUB_TOKEN"),
            patch.object(seam, "resolve_api_key", return_value=None),
            pytest.raises(RuntimeError, match="resolved to None"),
        ):
            resolve_occ_github_token(_CONTRACT)

    def test_app_mode_mints_and_never_reaches_the_pat_resolver(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv(GITHUB_AUTH_MODE_ENV_VAR, "app")
        with (
            patch.object(
                seam,
                "resolve_app_installation_token_from_contract",
                return_value="ghs_minted",
            ) as mock_app,
            patch.object(
                seam,
                "resolve_api_key",
                side_effect=AssertionError("reached GITHUB_TOKEN in app mode"),
            ) as mock_pat,
        ):
            assert resolve_occ_github_token(_CONTRACT) == "ghs_minted"
        mock_app.assert_called_once_with(_CONTRACT)
        mock_pat.assert_not_called()

    def test_app_mode_missing_credential_propagates(self, monkeypatch) -> None:
        monkeypatch.setenv(GITHUB_AUTH_MODE_ENV_VAR, "app")
        with (
            patch.object(
                seam,
                "resolve_app_installation_token_from_contract",
                side_effect=GitHubAppCredentialMissingError("ONEXBOT_OCC_APP_ID"),
            ),
            patch.object(seam, "resolve_api_key") as mock_pat,
            pytest.raises(GitHubAppCredentialMissingError, match="ONEXBOT_OCC_APP_ID"),
        ):
            resolve_occ_github_token(_CONTRACT)
        mock_pat.assert_not_called()


@pytest.mark.unit
class TestSingleDefinition:
    """AC6: one definition, so the halves of a mint cannot diverge again."""

    def test_every_occ_caller_dispatches_to_this_seam(self) -> None:
        import importlib

        for module_name in _SEAM_CALLERS:
            module = importlib.import_module(module_name)
            assert module._resolve_github_token.__module__ == module_name, (
                f"{module_name} lost its resolver"
            )
            assert module.resolve_occ_github_token is resolve_occ_github_token, (
                f"{module_name} does not dispatch to the shared seam"
            )

    def test_no_caller_redefines_the_auth_mode_switch(self) -> None:
        """The switch is spelled once, in this module, and read nowhere else.

        A caller that read the environment variable itself could silently
        disagree with the seam about what mode it is in -- the exact shape of
        the defect, where the write half thought it was in app mode and the
        read half had no opinion at all.
        """
        import importlib
        import inspect

        seam_source = inspect.getsource(seam)
        assert seam_source.count('GITHUB_AUTH_MODE_ENV_VAR = "') == 1

        for module_name in _SEAM_CALLERS:
            source = inspect.getsource(importlib.import_module(module_name))
            assert 'GITHUB_AUTH_MODE_ENV_VAR = "' not in source, (
                f"{module_name} redefines the auth-mode variable name"
            )
            assert "os.environ.get(GITHUB_AUTH_MODE_ENV_VAR" not in source, (
                f"{module_name} reads the auth mode itself instead of via the seam"
            )

    def test_every_occ_caller_declares_the_app_credential_refs(self) -> None:
        """A node resolving through the seam must declare what app mode reads.

        ``resolve_app_installation_token_from_contract`` reads the App secret
        names out of the CALLER's contract, so a caller that omits them cannot
        run in app mode at all -- which is the state the read half was in.
        """
        import importlib

        import yaml

        for module_name in _SEAM_CALLERS:
            module = importlib.import_module(module_name)
            contract = yaml.safe_load(Path(module._CONTRACT_PATH).read_text())
            declared = set(contract.get("secrets") or {})
            missing = {"ONEXBOT_OCC_APP_ID", "ONEXBOT_OCC_PRIVATE_KEY"} - declared
            assert not missing, f"{module_name} contract omits {sorted(missing)}"
