# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the shared OCC git transport helpers (OMN-13990).

Covers the HTTPS x-access-token URL construction, credential redaction, and the
run_git wrapper (success + credential-scrubbed failure). No network access.
"""

from __future__ import annotations

import subprocess

import pytest

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_git_transport import (
    OCC_REPO,
    authenticated_occ_url,
    run_git,
    scrub_credentials,
)


@pytest.mark.unit
class TestAuthenticatedOccUrl:
    def test_builds_x_access_token_https_url(self) -> None:
        url = authenticated_occ_url("ghp_faketoken")
        assert url == (
            f"https://x-access-token:ghp_faketoken@github.com/{OCC_REPO}.git"
        )
        assert url.startswith("https://x-access-token:")
        assert "git@github.com" not in url  # no SSH


@pytest.mark.unit
class TestScrubCredentials:
    def test_redacts_token_segment(self) -> None:
        text = (
            "fatal: unable to access "
            "'https://x-access-token:SUPERSECRET123@github.com/x/y.git'"
        )
        scrubbed = scrub_credentials(text)
        assert "SUPERSECRET123" not in scrubbed
        assert "x-access-token:***@github.com" in scrubbed

    def test_passthrough_when_no_credential(self) -> None:
        assert scrub_credentials("nothing secret here") == "nothing secret here"

    def test_empty_string_is_safe(self) -> None:
        assert scrub_credentials("") == ""


@pytest.mark.unit
class TestRunGit:
    def test_returns_stripped_stdout(self, tmp_path: object) -> None:
        result = run_git(["git", "--version"], cwd=str(tmp_path))  # type: ignore[arg-type]
        assert result.startswith("git version")

    def test_raises_called_process_error_on_failure(self, tmp_path: object) -> None:
        with pytest.raises(subprocess.CalledProcessError):
            run_git(
                ["git", "rev-parse", "--verify", "refs/heads/nope-xyz"],
                cwd=str(tmp_path),  # type: ignore[arg-type]
            )

    def test_credential_is_redacted_from_failure(self, tmp_path: object) -> None:
        # A locally-failing git invocation whose argv carries the token pattern:
        # git treats the first arg as a subcommand, fails fast (no network), and
        # the token must NOT leak into the raised exception.
        token_arg = "x-access-token:SUPERSECRET123@bogus"
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            run_git(
                ["git", token_arg, "definitely-not-a-git-subcommand"],
                cwd=str(tmp_path),  # type: ignore[arg-type]
            )
        rendered = (
            str(exc_info.value)
            + str(exc_info.value.cmd)
            + str(exc_info.value.stderr or "")
        )
        assert "SUPERSECRET123" not in rendered
        assert "x-access-token:***@bogus" in str(exc_info.value.cmd)
