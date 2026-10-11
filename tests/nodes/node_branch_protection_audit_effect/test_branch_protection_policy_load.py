# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The audited fleet comes from a deployment overlay, never from this package.

A key the overlay carries replaces the (empty) default; a key it leaves out
keeps the default. An overlay that is required and absent, unreadable, of the
wrong schema, carrying an unknown key or a non-list value, or naming a
repository as both release-synced and main-exempt is refused, so the audit
never runs over a default fleet and reads green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_branch_protection_audit_effect.handlers.handler_branch_protection_policy_load import (
    BranchProtectionOverlayError,
    HandlerBranchProtectionPolicyLoad,
)
from omnimarket.nodes.node_branch_protection_audit_effect.models import (
    OVERLAY_SCHEMA_VERSION,
    ModelBranchProtectionAuditPolicy,
    ModelBranchProtectionPolicyLoadRequest,
)

pytestmark = pytest.mark.unit


def _load(
    path: Path | None, *, required: bool = False
) -> ModelBranchProtectionAuditPolicy:
    return HandlerBranchProtectionPolicyLoad().handle(
        ModelBranchProtectionPolicyLoadRequest(
            overlay_path=path, overlay_required=required
        )
    )


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "overlay.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_shipped_default_fleet_is_empty() -> None:
    assert _load(None) == ModelBranchProtectionAuditPolicy()
    assert ModelBranchProtectionAuditPolicy().repos == ()


def test_overlay_lists_replace_defaults_and_absent_keys_keep_them(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        f'schema_version: "{OVERLAY_SCHEMA_VERSION}"\n'
        "repos:\n  - repo_a\n  - repo_b\n"
        "release_synced_main_repos:\n  - repo_a\n",
    )
    policy = _load(path, required=True)
    assert policy.repos == ("repo_a", "repo_b")
    assert policy.release_synced_main_repos == ("repo_a",)
    assert policy.private_repos == ()


def test_required_overlay_without_a_path_is_refused() -> None:
    with pytest.raises(BranchProtectionOverlayError, match="required"):
        _load(None, required=True)


def test_named_overlay_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BranchProtectionOverlayError, match="not found"):
        _load(tmp_path / "absent.yaml")


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("- just\n- a list\n", "mapping"),
        ('schema_version: "other.v1"\nrepos: [a]\n', "schema_version"),
        ("repos: [a]\n", "schema_version"),
        (f'schema_version: "{OVERLAY_SCHEMA_VERSION}"\nrepo: [a]\n', "unknown key"),
        (f'schema_version: "{OVERLAY_SCHEMA_VERSION}"\nrepos: a\n', "list"),
        (f'schema_version: "{OVERLAY_SCHEMA_VERSION}"\nrepos: [1]\n', "list"),
        (f'schema_version: "{OVERLAY_SCHEMA_VERSION}"\nrepos: [a\n', "YAML"),
    ],
)
def test_invalid_overlay_is_refused(tmp_path: Path, text: str, reason: str) -> None:
    with pytest.raises(BranchProtectionOverlayError, match=reason):
        _load(_write(tmp_path, text))


def test_release_synced_and_main_exempt_sets_are_disjoint(tmp_path: Path) -> None:
    """A repo cannot be both release-synced-asserted and unaudited on main."""
    path = _write(
        tmp_path,
        f'schema_version: "{OVERLAY_SCHEMA_VERSION}"\n'
        "release_synced_main_repos: [repo_a]\n"
        "main_audit_exempt_repos: [repo_a]\n",
    )
    with pytest.raises(BranchProtectionOverlayError, match="repo_a"):
        _load(path)
