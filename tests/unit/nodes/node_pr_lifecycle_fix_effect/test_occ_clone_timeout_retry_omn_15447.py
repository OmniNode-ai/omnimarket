# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15447: retry only transient clone timeouts within the node budget."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers import (
    occ_companion_emitter as module,
)


@pytest.mark.unit
class TestOccCloneTimeoutRetry:
    def test_timeout_then_success_removes_partial_clone(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        emitter = module.OccCompanionEmitter()
        clone_dir = tmp_path / "onex_change_control"
        base_sha = "a" * 40
        token = "test-secret-token"
        clone_attempts = 0

        def fake_git(argv: list[str], *, cwd: str, strip: bool = True) -> str:
            nonlocal clone_attempts
            if argv[:2] == ["git", "clone"]:
                clone_attempts += 1
                assert cwd == str(tmp_path)
                assert argv[2] == "--depth=1"
                assert argv[-1] == str(clone_dir)
                if clone_attempts == 1:
                    clone_dir.mkdir()
                    (clone_dir / "partial-pack").write_text("incomplete")
                    raise subprocess.TimeoutExpired(argv, module._GIT_TIMEOUT_SECONDS)
                assert not clone_dir.exists()
                clone_dir.mkdir()
            return ""

        with (
            patch.object(emitter, "_run_git", side_effect=fake_git) as git,
            patch.object(emitter, "_head_sha", return_value=base_sha) as head,
            caplog.at_level(logging.WARNING, logger=module.__name__),
        ):
            result = emitter._clone_and_branch(
                clone_dir, "companion-test", str(tmp_path), token
            )

        assert result == base_sha
        assert clone_attempts == 2
        after_clone = [(c.args[0], c.kwargs["cwd"]) for c in git.call_args_list[2:]]
        assert after_clone == [
            (["git", "config", "user.name", emitter._git_author_name], str(clone_dir)),
            (
                ["git", "config", "user.email", emitter._git_author_email],
                str(clone_dir),
            ),
            (["git", "checkout", "-b", "companion-test"], str(clone_dir)),
        ]
        head.assert_called_once_with(str(clone_dir))
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING
        assert str(module._GIT_TIMEOUT_SECONDS) in caplog.text
        assert token not in caplog.text
        assert "https://" not in caplog.text

    def test_final_timeout_propagates_unchanged(self, tmp_path: Path) -> None:
        emitter = module.OccCompanionEmitter()
        timeouts: list[subprocess.TimeoutExpired] = []

        def timeout(argv: list[str], *, cwd: str, strip: bool = True) -> str:
            assert argv[:2] == ["git", "clone"]
            error = subprocess.TimeoutExpired(argv, module._GIT_TIMEOUT_SECONDS)
            timeouts.append(error)
            raise error

        with (
            patch.object(emitter, "_run_git", side_effect=timeout) as git,
            patch.object(emitter, "_head_sha") as head,
            pytest.raises(subprocess.TimeoutExpired) as raised,
        ):
            emitter._clone_and_branch(
                tmp_path / "onex_change_control",
                "companion-test",
                str(tmp_path),
                "token",
            )

        assert git.call_count == 2
        assert git.call_count == module._CLONE_MAX_ATTEMPTS
        assert raised.value is timeouts[-1]
        head.assert_not_called()

    def test_non_timeout_is_not_retried(self, tmp_path: Path) -> None:
        emitter = module.OccCompanionEmitter()
        error = subprocess.CalledProcessError(128, ["git", "clone"])

        with (
            patch.object(emitter, "_run_git", side_effect=error) as git,
            patch.object(emitter, "_head_sha") as head,
            pytest.raises(subprocess.CalledProcessError) as raised,
        ):
            emitter._clone_and_branch(
                tmp_path / "onex_change_control",
                "companion-test",
                str(tmp_path),
                "token",
            )

        git.assert_called_once()
        assert git.call_args.args[0][:2] == ["git", "clone"]
        assert raised.value is error
        head.assert_not_called()

    def test_clone_attempts_fit_contract_timeout(self) -> None:
        contract_path = Path(module.__file__).resolve().parents[1] / "contract.yaml"
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        clone_budget_ms = (
            module._CLONE_MAX_ATTEMPTS * module._GIT_TIMEOUT_SECONDS * 1000
        )

        assert module._CLONE_MAX_ATTEMPTS >= 2
        assert clone_budget_ms < contract["descriptor"]["timeout_ms"]
