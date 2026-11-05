# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded relocation failures and routing DB binding behavior."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
import yaml
from omnibase_core.validation.validator_url_authority import scan_source

from omnimarket.handlers.routing import (
    dod_overlay,
    roi_overlay,
    tenant_overlay_resolver,
)

_ROOT = Path(__file__).resolve().parents[3]
_MODULES = (dod_overlay, roi_overlay, tenant_overlay_resolver)


@pytest.mark.parametrize("module", _MODULES)
def test_relocated_readers_have_no_url_authority_violations(module: ModuleType) -> None:
    path = Path(module.__file__)
    violations = scan_source(
        "omnimarket", path.relative_to(_ROOT).as_posix(), path.read_text()
    )
    assert violations == []


def test_sources_follow_the_declared_binding_instead_of_ambient_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = tmp_path / "sources.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "routing_overlay_sources": {
                    "projection": {"dsn_env": "TEST_ROUTING_PROJECTION_DSN"},
                    "verdict": {"dsn_env": "TEST_ROUTING_VERDICT_DSN"},
                }
            }
        )
    )
    monkeypatch.setenv("DELEGATION_ROUTING_SOURCES_PATH", str(overlay))
    monkeypatch.setenv(
        "TEST_ROUTING_PROJECTION_DSN", "  postgresql:///routing_projection  "
    )
    monkeypatch.setenv("TEST_ROUTING_VERDICT_DSN", "  postgresql:///routing_verdict  ")
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql:///wrong_projection")
    monkeypatch.setenv("OMNINODE_INTERNAL_DB_URL", "postgresql:///wrong_verdict")
    monkeypatch.setattr(
        roi_overlay, "resolve_rls_read_tenant", lambda *_args, **_kwargs: "tenant"
    )
    from omnimarket.projection import postgres_read_database

    monkeypatch.setattr(
        postgres_read_database,
        "PostgresReadDatabaseAdapter",
        lambda dsn, **_kwargs: dsn,
    )

    assert roi_overlay.resolve_context_roi_db() == "postgresql:///routing_projection"
    assert (
        tenant_overlay_resolver.resolve_tenant_overlay_db()
        == "postgresql:///routing_projection"
    )
    eval_reader = dod_overlay.resolve_eval_line_reader()
    assert eval_reader is not None
    assert eval_reader._dsn == "postgresql:///routing_projection"
    dod_reader = dod_overlay.resolve_dod_outcome_reader()
    assert dod_reader is not None
    assert dod_reader._delegation_dsn == "postgresql:///routing_projection"
    assert dod_reader._verdict_dsn == "postgresql:///routing_verdict"

    monkeypatch.delenv("TEST_ROUTING_PROJECTION_DSN")
    assert roi_overlay.resolve_context_roi_db() is None
    assert tenant_overlay_resolver.resolve_tenant_overlay_db() is None
    assert dod_overlay.resolve_eval_line_reader() is None
    assert dod_overlay.resolve_dod_outcome_reader() is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"projection": {"dsn_env": "X"}},
        {"projection": {"dsn_env": "X"}, "verdict": {"dsn_env": "Y"}, "extra": {}},
    ],
)
def test_invalid_explicit_sources_disable_optional_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: object
) -> None:
    overlay = tmp_path / "invalid.yaml"
    overlay.write_text(yaml.safe_dump({"routing_overlay_sources": raw}))
    monkeypatch.setenv("DELEGATION_ROUTING_SOURCES_PATH", str(overlay))
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql:///ambient_projection")
    monkeypatch.setenv("OMNINODE_INTERNAL_DB_URL", "postgresql:///ambient_verdict")
    assert roi_overlay.resolve_context_roi_db() is None
    assert tenant_overlay_resolver.resolve_tenant_overlay_db() is None
    assert dod_overlay.resolve_eval_line_reader() is None
    assert dod_overlay.resolve_dod_outcome_reader() is None
