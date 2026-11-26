# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Strict deployment overlay precedence and schema validation (OMN-21622)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_principal_grant_audit_effect.handlers.handler_principal_grant_audit import (
    OVERLAY_ENV,
    PrincipalGrantAuditConfigurationError,
    load_deployment_overlay,
)
from omnimarket.nodes.node_principal_grant_audit_effect.models import (
    ModelPrincipalGrantAuditDeployment,
)

pytestmark = pytest.mark.unit

FULL = {
    "infra_checkout": "repo",
    "script": "scripts/audit.py",
    "grants_file": "grants.yaml",
    "issuer_container": "container-a",
    "overlay_mount_target": "/overlay/doc.yaml",
    "ledger_file": "ledger.jsonl",
    "role": "machine",
    "timeout_seconds": 300,
}


@pytest.fixture(autouse=True)
def clear_overlay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OVERLAY_ENV, raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)


def _write(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_pointer_wins_over_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pointer = _write(tmp_path / "explicit.yaml", FULL)
    _write(
        tmp_path / "root" / "node_principal_grant_audit_effect" / "overlay.yaml", FULL
    )
    monkeypatch.setenv(OVERLAY_ENV, str(pointer))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "root"))
    deployment = load_deployment_overlay()
    assert isinstance(deployment, ModelPrincipalGrantAuditDeployment)
    assert deployment.infra_checkout == FULL["infra_checkout"]


def test_roots_use_first_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(
        tmp_path / "first" / "node_principal_grant_audit_effect" / "overlay.yaml", FULL
    )
    _write(
        tmp_path / "second" / "node_principal_grant_audit_effect" / "overlay.yaml", FULL
    )
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        os.pathsep.join(
            (
                "",
                str(tmp_path / "missing"),
                str(tmp_path / "first"),
                "",
                str(tmp_path / "second"),
            )
        ),
    )
    assert load_deployment_overlay() is not None


def test_no_overlay_resolves_none() -> None:
    assert load_deployment_overlay() is None


@pytest.mark.parametrize(
    "bad",
    [
        {k: v for k, v in FULL.items() if k != "infra_checkout"},
        {**FULL, "unexpected": True},
        {**FULL, "infra_checkout": ""},
        {**FULL, "timeout_seconds": True},
        {**FULL, "timeout_seconds": 0},
        {**FULL, "timeout_seconds": -1},
        {**FULL, "timeout_seconds": "300"},
    ],
)
def test_invalid_documents_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: dict[str, object]
) -> None:
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(bad), encoding="utf-8")
    monkeypatch.setenv(OVERLAY_ENV, str(path))
    with pytest.raises(PrincipalGrantAuditConfigurationError, match=OVERLAY_ENV):
        load_deployment_overlay()


def test_non_mapping_document_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "overlay.yaml"
    path.write_text("null\n", encoding="utf-8")
    monkeypatch.setenv(OVERLAY_ENV, str(path))
    with pytest.raises(PrincipalGrantAuditConfigurationError, match=OVERLAY_ENV):
        load_deployment_overlay()
