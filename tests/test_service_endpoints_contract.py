# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the service-endpoint authority (OMN-12806).

Verifies:
1. The YAML config loads and exposes the three canonical URL constants.
2. The loader raises fail-closed errors on missing / malformed keys.
3. All omnimarket source modules no longer contain bare hardcoded
   api.github.com or api.linear.app URL string literals (the ``# url-authority-ok``
   annotation exempts intentional one-off overrides).
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest

import omnimarket.config.service_endpoints as _ep_module
from omnimarket.config.service_endpoints import (
    GITHUB_GRAPHQL_URL,
    GITHUB_REST_URL,
    LINEAR_GRAPHQL_URL,
    _load_config,
    _require,
    service_endpoint_url,
)

# ---------------------------------------------------------------------------
# 1. Canonical URL values
# ---------------------------------------------------------------------------


class TestCanonicalUrls:
    def test_github_rest_url(self) -> None:
        assert GITHUB_REST_URL == "https://api.github.com"

    def test_github_graphql_url(self) -> None:
        assert GITHUB_GRAPHQL_URL == "https://api.github.com/graphql"

    def test_github_web_url(self) -> None:
        assert service_endpoint_url("github.web_url") == "https://github.com"

    def test_linear_graphql_url(self) -> None:
        assert LINEAR_GRAPHQL_URL == "https://api.linear.app/graphql"

    def test_all_urls_are_https(self) -> None:
        for url in (
            GITHUB_REST_URL,
            GITHUB_GRAPHQL_URL,
            LINEAR_GRAPHQL_URL,
        ):
            assert url.startswith("https://"), f"URL must be HTTPS: {url}"

    def test_config_file_exists(self) -> None:
        assert _ep_module._CONFIG_PATH.exists(), (
            f"service_endpoints.yaml not found at {_ep_module._CONFIG_PATH}"
        )


# ---------------------------------------------------------------------------
# 2. Fail-closed loader behaviour
# ---------------------------------------------------------------------------


class TestLoadConfigFailClosed:
    def test_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(
            FileNotFoundError, match=r"service_endpoints\.yaml not found"
        ):
            _load_config(tmp_path / "nonexistent.yaml")

    def test_missing_github_key_raises_key_error(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "service_endpoints.yaml"
        cfg_file.write_text(
            textwrap.dedent("""\
                linear:
                  graphql_url: "https://api.linear.app/graphql"
            """),
            encoding="utf-8",
        )
        raw = _load_config(cfg_file)
        with pytest.raises(KeyError, match="github"):
            _require(raw, "github", "rest_url")

    def test_empty_url_raises_key_error(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "service_endpoints.yaml"
        cfg_file.write_text(
            textwrap.dedent("""\
                github:
                  rest_url: ""
                  graphql_url: "https://api.github.com/graphql"
                linear:
                  graphql_url: "https://api.linear.app/graphql"
            """),
            encoding="utf-8",
        )
        raw = _load_config(cfg_file)
        with pytest.raises(KeyError, match="non-empty string"):
            _require(raw, "github", "rest_url")

    def test_non_string_value_raises_key_error(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "service_endpoints.yaml"
        cfg_file.write_text(
            textwrap.dedent("""\
                github:
                  rest_url: 42
                  graphql_url: "https://api.github.com/graphql"
                linear:
                  graphql_url: "https://api.linear.app/graphql"
            """),
            encoding="utf-8",
        )
        raw = _load_config(cfg_file)
        with pytest.raises(KeyError, match="non-empty string"):
            _require(raw, "github", "rest_url")

    def test_root_non_mapping_raises_key_error(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "service_endpoints.yaml"
        cfg_file.write_text("- item1\n- item2\n", encoding="utf-8")
        with pytest.raises(KeyError, match="must be a mapping"):
            _load_config(cfg_file)


# ---------------------------------------------------------------------------
# 3. No bare hardcoded URL literals remain in omnimarket source
# ---------------------------------------------------------------------------

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "omnimarket"

# Pattern matches bare string literals containing the forbidden domains.
# Exemptions:
#   - Lines ending with ``# url-authority-ok:…`` (pre-existing approved carve-outs)
#   - Lines in test files (tests may legitimately reference the URLs as expected values)
#   - The service_endpoints.yaml file itself (canonical declaration)
#   - The service_endpoints.py loader (reads the YAML; its string literals are
#     error messages, not URLs)
_FORBIDDEN_URL_PATTERN = re.compile(
    r'"https?://(api\.github\.com|api\.linear\.app)[\w/]*"'
)
_EXEMPTION_COMMENT = re.compile(r"#\s*url-authority-ok")


def _collect_violations() -> list[str]:
    violations: list[str] = []
    for py_file in _SRC_ROOT.rglob("*.py"):
        # Exempt the loader itself
        if py_file.name == "service_endpoints.py" and py_file.parent.name == "config":
            continue
        if "tests" in py_file.relative_to(_SRC_ROOT).parts:
            continue
        for lineno, line in enumerate(
            py_file.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _FORBIDDEN_URL_PATTERN.search(line) and not _EXEMPTION_COMMENT.search(
                line
            ):
                violations.append(
                    f"{py_file.relative_to(_SRC_ROOT)}:{lineno}: {line.strip()}"
                )
    return violations


@pytest.mark.unit
def test_no_bare_hardcoded_external_service_urls() -> None:
    """All api.github.com / api.linear.app literals must be removed from source.

    Allowed exceptions:
    - Lines annotated with ``# url-authority-ok: <reason>``
    - The service_endpoints.py loader (exempted above)
    """
    violations = _collect_violations()
    assert not violations, (
        "Hardcoded external-service URL literals found in omnimarket source.\n"
        "Move to configs/service_endpoints.yaml and import from "
        "omnimarket.config.service_endpoints.\n"
        "If this is a legitimate one-off, append ``# url-authority-ok: <reason>``.\n\n"
        + "\n".join(violations)
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "reference", ["missing.graphql_url", "linear.missing", "linear", ""]
)
def test_endpoint_reference_fails_closed(reference: str) -> None:
    with pytest.raises(KeyError):
        _ep_module.service_endpoint_url(reference)


@pytest.mark.unit
def test_endpoint_reference_resolves_shared_authority() -> None:
    assert _ep_module.service_endpoint_url("linear.graphql_url") == LINEAR_GRAPHQL_URL
    assert (
        _ep_module.service_endpoint_url("slack.auth_test_url")
        == _ep_module.SLACK_AUTH_TEST_URL
    )


# ---------------------------------------------------------------------------
# 4. The GitHub web origin is read from the endpoint authority, never spelled
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GITHUB_WEB_RESOLVED_MODULES = (
    "src/omnimarket/nodes/node_contractor_integration_note_effect/services/note_composer.py",
    "src/omnimarket/nodes/node_git_query_mirror_effect/git_mirror.py",
    "src/omnimarket/nodes/node_lab_work_unit_effect/protocols/local_shell_work_runner.py",
    "src/omnimarket/nodes/node_linear_triage/services/local_github_pr_reads.py",
    "src/omnimarket/nodes/node_local_dashboard_serve_effect/bundle.py",
    "src/omnimarket/nodes/node_pr_head_stage_compute/handlers/handler_pr_head_stage.py",
    "src/omnimarket/nodes/node_push_validation_effect/protocols/ephemeral_container_focused_run_subprocess.py",
    "src/omnimarket/nodes/node_push_validation_effect/protocols/gate_runner_suite_evaluation_subprocess.py",
    "src/omnimarket/nodes/node_push_validation_effect/protocols/git_push_validation_subprocess.py",
    "src/omnimarket/nodes/node_remote_lane_effect/handlers/handler_remote_lane_ref.py",
)


def _string_constants(tree: ast.AST) -> list[str]:
    """Every string constant in the module that is code, not a docstring."""
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


class TestGithubWebOriginIsResolved:
    @pytest.mark.parametrize("relative", _GITHUB_WEB_RESOLVED_MODULES)
    def test_module_holds_no_github_web_literal(self, relative: str) -> None:
        tree = ast.parse((_REPO_ROOT / relative).read_text(encoding="utf-8"))
        offenders = [
            value for value in _string_constants(tree) if "https://github.com" in value
        ]
        assert offenders == [], f"{relative} spells the GitHub web origin: {offenders}"
