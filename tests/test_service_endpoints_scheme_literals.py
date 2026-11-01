# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Origins and transport sentinels are read from the endpoint authority, never spelled.

Covers the literal_url slice of the contracts-and-DI burn-down (OMN-21693): each
module below used to spell a web origin, a schema dialect URI or a transport
sentinel in code. They now resolve it from ``configs/service_endpoints.yaml``
through ``service_endpoint_url``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from omnimarket.config.service_endpoints import (
    GITHUB_REST_URL,
    service_endpoint_url,
)
from omnimarket.nodes.node_cloud_session_placement_effect.handlers import (
    cloud_accounts,
    cloud_session,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_evidence_github_effect import (
    PACKAGE_INDEX_JSON_URL,
)
from omnimarket.nodes.node_legacy_worktree_audit_effect.handlers.handler_legacy_worktree_audit import (
    remote_to_slug,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

_RESOLVED_MODULES = (
    "src/omnimarket/nodes/node_build_loop_orchestrator/handlers/adapter_delegation_router.py",
    "src/omnimarket/nodes/node_cloud_session_placement_effect/handlers/cloud_accounts.py",
    "src/omnimarket/nodes/node_cloud_session_placement_effect/handlers/cloud_session.py",
    "src/omnimarket/nodes/node_codex_landing_compute/handlers/handler_codex_landing.py",
    "src/omnimarket/nodes/node_contract_graph_ir_compute/handlers/handler_contract_graph_ir.py",
    "src/omnimarket/nodes/node_dod_verify/handlers/handler_dod_evidence_github_effect.py",
    "src/omnimarket/nodes/node_dod_verify/services/durable_evidence_gate.py",
    "src/omnimarket/nodes/node_dod_verify/services/evidence_collector.py",
    "src/omnimarket/nodes/node_dod_verify/services/local_gh_reads.py",
    "src/omnimarket/nodes/node_legacy_worktree_audit_effect/handlers/handler_legacy_worktree_audit.py",
    "src/omnimarket/nodes/node_morning_ground_state_orchestrator/tools/effects.py",
    "src/omnimarket/nodes/node_plans_today_effect/models/model_plans_today_schema.py",
    "src/omnimarket/nodes/node_prod_promotion_grant_resolver_effect/handlers/handler_prod_promotion_grant_resolver.py",
)

_SPELLED_ORIGIN = re.compile(
    r"https?://(?:github\.com|claude\.ai|pypi\.org|omninode\.ai|json-schema\.org)"
    r"|github\\\.com|claude\\\.ai"
    r"|cli://gemini|local://inmemory|in-memory://ui_component"
)


def _code_strings(tree: ast.AST) -> list[str]:
    """Every string constant in the module that is code, not a docstring or comment."""
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


@pytest.mark.unit
@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("github.web_url", "https://github.com"),
        ("pypi.base_url", "https://pypi.org"),
        ("claude.web_url", "https://claude.ai"),
        (
            "json_schema.draft_2020_12_url",
            "https://json-schema.org/draft/2020-12/schema",
        ),
        ("transport_sentinels.gemini_cli_url", "cli://gemini"),
        (
            "transport_sentinels.inmemory_ui_component_prefix",
            "in-memory://ui_component/",
        ),
    ],
)
def test_authority_declares_the_value(reference: str, expected: str) -> None:
    assert service_endpoint_url(reference) == expected


@pytest.mark.unit
@pytest.mark.parametrize("relative", _RESOLVED_MODULES)
def test_module_spells_no_origin_or_sentinel(relative: str) -> None:
    tree = ast.parse((_REPO_ROOT / relative).read_text(encoding="utf-8"))
    offenders = [v for v in _code_strings(tree) if _SPELLED_ORIGIN.search(v)]
    assert offenders == [], f"{relative} spells an origin or sentinel: {offenders}"


@pytest.mark.unit
def test_package_index_url_keeps_its_shape() -> None:
    assert (
        PACKAGE_INDEX_JSON_URL == "https://pypi.org/pypi/{distribution}/{version}/json"
    )


@pytest.mark.unit
def test_cloud_session_url_regex_matches_the_claude_origin_only() -> None:
    assert cloud_accounts.CLOUD_URL_RE.search(
        "see https://claude.ai/code/session_abc123XYZ"
    )
    assert (
        cloud_accounts.CLOUD_URL_RE.search("https://claudeXai/code/session_abc") is None
    )


@pytest.mark.unit
def test_cloud_pr_url_reads_the_github_origin_from_the_authority() -> None:
    out = "noise\nhttps://github.com/acme/widget/pull/12\n"
    assert cloud_session.cloud_pr_url(out, "acme", "widget") == (
        "https://github.com/acme/widget/pull/12"
    )
    assert cloud_session.cloud_pr_url(out, "acme", "other") is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.net/v1/",
        "https://api.example.net/v1",
        "https://api.example.net/v1/?x=1",
        "https://api.example.net/v1/#frag",
        "https://api.example.net:8443/v1/",
        "https://api.example.net/v 1/",
        "ftp://api.example.net/v1/",
    ],
)
def test_cloud_session_script_refuses_a_malformed_sessions_url(url: str) -> None:
    with pytest.raises(ValueError, match="not a sessions API URL"):
        cloud_session.cloud_session_script(
            "export PATH=/usr/bin", "session_abc12345", url
        )


@pytest.mark.unit
def test_cloud_session_script_accepts_a_well_formed_sessions_url() -> None:
    script = cloud_session.cloud_session_script(
        "export PATH=/usr/bin",
        "session_abc12345",
        "https://api.example.net/v1/sessions/",
    )
    assert "https://api.example.net/v1/sessions/" in script


@pytest.mark.unit
@pytest.mark.parametrize(
    ("remote", "slug"),
    [
        ("https://github.com/acme/widget.git", "acme/widget"),
        ("git@github.com:acme/widget.git", "acme/widget"),
        ("https://gitlab.example/acme/widget.git", ""),
        ("", ""),
    ],
)
def test_remote_to_slug_reads_the_github_origin(remote: str, slug: str) -> None:
    assert remote_to_slug(remote) == slug


@pytest.mark.unit
def test_rest_origin_is_the_authority_value() -> None:
    from omnimarket.nodes.node_prod_promotion_grant_resolver_effect.handlers import (
        handler_prod_promotion_grant_resolver as resolver,
    )

    assert resolver._GITHUB_API_BASE == GITHUB_REST_URL
