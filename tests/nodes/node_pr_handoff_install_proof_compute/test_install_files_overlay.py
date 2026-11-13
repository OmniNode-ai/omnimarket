# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: which changed files require an install proof (OMN-23616)."""

from __future__ import annotations

import pytest

from omnimarket.models.pr_handoff_install_proof import (
    ModelInstallDeclaration,
    ModelInstallProofRequest,
    ModelInstallProofSource,
)
from omnimarket.nodes.node_pr_handoff_install_proof_compute.handlers.handler_install_proof import (
    HandlerInstallProof,
)


@pytest.fixture
def declaration() -> ModelInstallDeclaration:
    return ModelInstallDeclaration(
        paths=(
            "launchd/*",
            "*/launchd/*",
            "systemd/*",
            "*/systemd/*",
            "*.plist",
            "*.plist.tmpl",
            "*.service",
            "*.service.tmpl",
            "*.timer",
            "*.timer.tmpl",
            "hooks/*",
            "*/hooks/*",
            "plugins/*",
            ".claude-plugin/*",
            "*/.claude-plugin/*",
            "scripts/*",
            "*/scripts/*",
            "bin/*",
            "*/bin/*",
        ),
        repo_paths={
            "repo-a": ("src/*",),
            "repo-b": ("src/*", "config/*"),
        },
        excluded_paths=(
            "tests/*",
            "*/tests/*",
            "docs/*",
            "*/docs/*",
            "README.md",
            "*/README.md",
            "CHANGELOG.md",
            "*/CHANGELOG.md",
            "contracts/*",
            "*.md.tmpl",
            "scripts/application-relation-ownership.yaml",
            "*/scripts/application-relation-ownership.yaml",
            "scripts/ci/*",
            "*/scripts/ci/*",
            "scripts/validation/*",
            "*/scripts/validation/*",
        ),
        prod_paths=(
            "grants/*",
            "*/grants/*",
            "prod/*",
            "*/prod/*",
            "*prod_promotion*",
            "*/manifests/prod*",
        ),
        installed_cell="installed",
        observed_cell="observed",
        observed_words=(
            "tick",
            "fired",
            "firing",
            "ran",
            "running",
            "loaded",
            "invoked",
            "called",
            "launchctl",
            "systemctl",
            "exit 0",
            "exit=0",
            "returned",
            "printed",
            "output",
            "started",
        ),
        observed_refused=(
            r"^\W*(\d+\s+)?(focused\s+|unit\s+)?(tests?|pytest|ci)\b[^.;]*\b(pass|passed|passes|green|ok)\W*$",
        ),
        min_observed_chars=12,
        ruling=(
            "operator 2026-10-31 (OMN-21815): a PR that changes a host install, a launchd or "
            "systemd unit, a tool, a hook or a plugin is handed off only with the branch "
            "revision installed on the lab and observed running; prod and anything that "
            "needs a grant are excluded"
        ),
    )


def _handler(declaration: ModelInstallDeclaration) -> HandlerInstallProof:
    return HandlerInstallProof(declaration_loader=lambda: declaration)


def test_ci_script_is_na(declaration: ModelInstallDeclaration) -> None:
    handler = _handler(declaration)
    result = handler.handle(
        ModelInstallProofRequest(
            repo="repo-a", head="abc1234", files=("scripts/ci/check_x.py",)
        )
    )
    assert result.verdict == "NA"


def test_tool_script_is_no(declaration: ModelInstallDeclaration) -> None:
    handler = _handler(declaration)
    result = handler.handle(
        ModelInstallProofRequest(
            repo="repo-a", head="abc1234", files=("scripts/tool.sh",)
        )
    )
    assert result.verdict == "NO"
    assert "scripts/tool.sh" in result.detail
    assert "scripts/tool.sh" in result.required_files


def test_launchd_plist_is_no(declaration: ModelInstallDeclaration) -> None:
    handler = _handler(declaration)
    result = handler.handle(
        ModelInstallProofRequest(
            repo="repo-a", head="abc1234", files=("launchd/x.plist",)
        )
    )
    assert result.verdict == "NO"
    assert "launchd/x.plist" in result.required_files


def test_validation_and_test_files_are_na(declaration: ModelInstallDeclaration) -> None:
    handler = _handler(declaration)
    result = handler.handle(
        ModelInstallProofRequest(
            repo="repo-a",
            head="abc1234",
            files=("scripts/validation/v.py", "tests/test_a.py"),
        )
    )
    assert result.verdict == "NA"


def test_repo_paths_hit_is_no(declaration: ModelInstallDeclaration) -> None:
    handler = _handler(declaration)
    result = handler.handle(
        ModelInstallProofRequest(repo="repo-a", head="abc1234", files=("src/a.py",))
    )
    assert result.verdict == "NO"
    assert "src/a.py" in result.required_files


def test_repo_paths_miss_is_na(declaration: ModelInstallDeclaration) -> None:
    handler = _handler(declaration)
    result = handler.handle(
        ModelInstallProofRequest(repo="other-repo", head="abc1234", files=("src/a.py",))
    )
    assert result.verdict == "NA"


def test_installed_and_observed_is_ok(declaration: ModelInstallDeclaration) -> None:
    handler = _handler(declaration)
    result = handler.handle(
        ModelInstallProofRequest(
            repo="repo-a",
            head="abc1234",
            files=("launchd/x.plist",),
            sources=(
                ModelInstallProofSource(
                    name="lab",
                    text=(
                        "Lab: head=abc1234 installed=h201:abc1234 "
                        "observed=the unit ran and printed output"
                    ),
                ),
            ),
        )
    )
    assert result.verdict == "OK"
    assert "installed=h201:abc1234" in result.detail
    assert "source=lab" in result.detail
