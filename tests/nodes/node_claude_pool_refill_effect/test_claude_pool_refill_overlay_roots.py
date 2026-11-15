# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the overlay_roots key of node_claude_pool_refill_effect (OMN-24580).

The overlay's optional ``overlay_roots`` list is carried into the remote shell as
``ONEX_SKILL_OVERLAY_ROOTS`` under the registry root the prelude finds, so the pool's
``status --json`` can read its overlay on the host.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_claude_pool_refill_effect.handlers import (
    handler_claude_pool_refill as refill_mod,
)
from omnimarket.nodes.node_claude_pool_refill_effect.models.model_claude_pool_refill import (
    ModelClaudePoolRefillRequest,
)
from omnimarket.nodes.node_claude_pool_refill_effect.models.model_claude_pool_refill_overlay import (
    ModelClaudePoolRefillOverlay,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolEntry,
    ModelClaudeTokenPoolResult,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool_status_view import (
    ModelPoolStatusViewResult,
)

pytestmark = pytest.mark.unit

_OVERLAY = """
store:
  project_id: 00000000-0000-0000-0000-000000000000
  environment: dev
  folder: /shared/pool
  reader_host: hr
  identity_env_file: ~/.id.env
hosts_table: hosts.yaml
tokens:
  - {label: a, key: K_A}
hosts: [h1]
venv_roots: ["$HOME/root"]
probe:
  model: m
  prompt: say ok
  timeout_s: 5
  limited_seconds: 60
  patterns:
    refused: ["organization has disabled"]
    expired: ["oauth token revoked"]
    limited: ["hit your"]
"""


@pytest.fixture(autouse=True)
def _no_ambient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    monkeypatch.delenv("ONEX_LAB_RUN_HOSTS", raising=False)
    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)


def _overlay(tmp_path: Path, extra: str = "") -> Any:
    lab = tmp_path / refill_mod.OVERLAY_REL.parent
    lab.mkdir(parents=True)
    (tmp_path / "hosts.yaml").write_text(
        "hosts:\n  - {name: hr, target: reader}\n  - {name: h1, target: one}\n",
        encoding="utf-8",
    )
    path = lab / refill_mod.OVERLAY_REL.name
    path.write_text(_OVERLAY + extra, encoding="utf-8")
    return refill_mod.load_overlay(path)


def test_prelude_exports_the_overlay_roots_under_the_registry_root_it_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "registry"
    venv = registry / ".onex-dispatch-venv" / "bin"
    venv.mkdir(parents=True)
    py = venv / "python"
    py.write_text("#!/bin/sh\n", encoding="utf-8")
    py.chmod(0o755)
    ov = _overlay(
        tmp_path,
        extra="\noverlay_roots: [overlays/overlays, plugins/extra/overlays]\n",
    )
    ov = dataclasses.replace(ov, venv_roots=[str(registry)])
    script = refill_mod._prelude(ov) + 'echo "$ONEX_SKILL_OVERLAY_ROOTS"\n'
    env = {k: v for k, v in os.environ.items() if k != "ONEX_SKILL_OVERLAY_ROOTS"}
    out = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    expected = ":".join(f"{registry}/{r}" for r in ov.overlay_roots)
    assert out.stdout.strip() == expected


def test_without_overlay_roots_the_prelude_is_unchanged(tmp_path: Path) -> None:
    ov = _overlay(tmp_path)
    text = refill_mod._prelude(ov)
    assert "ONEX_SKILL_OVERLAY_ROOTS" not in text


@pytest.mark.parametrize(
    "bad",
    ["/abs/path", "../up", "a;b", "~/x"],
)
def test_unsafe_overlay_roots_are_refused(tmp_path: Path, bad: str) -> None:
    with pytest.raises(refill_mod.ClaudePoolRefillRefusedError, match=re.escape(bad)):
        _overlay(tmp_path, extra=f"\noverlay_roots: [{bad!s}]\n".replace("'", '"'))


def test_host_pool_runs_the_status_with_the_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ov = _overlay(
        tmp_path,
        extra="\noverlay_roots: [overlays/overlays]\n",
    )
    seen: list[str] = []

    def fake_run(host: Any, script: str, *, timeout: float = 300) -> Any:
        seen.append(script)
        return subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"tokens": [{"label": "a"}]}), stderr=""
        )

    monkeypatch.setattr(refill_mod, "_run", fake_run)
    result = refill_mod.host_pool(ov, ov.hosts[0])
    assert result == {"a": {"label": "a"}}
    assert "ONEX_SKILL_OVERLAY_ROOTS" in seen[0]
    assert f"-m {refill_mod.POOL_MODULE} status --json" in seen[0]


def test_host_pool_returns_the_pool_entries_with_fingerprints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ov = _overlay(tmp_path)
    pool = ModelClaudeTokenPoolResult(
        operation="status",
        host="h1",
        status_line="ok",
        tokens=(
            ModelClaudeTokenPoolEntry(
                label="a",
                fingerprint="fp-a",
                added_at="2026-01-01T00:00:00Z",
                revoked=False,
                limited=False,
            ),
            ModelClaudeTokenPoolEntry(
                label="b",
                fingerprint="fp-b",
                added_at="2026-01-02T00:00:00Z",
                revoked=True,
                limited=False,
            ),
        ),
    )

    def fake_run(host: Any, script: str, *, timeout: float = 300) -> Any:
        assert "status --json --entries" in script
        return subprocess.CompletedProcess(
            [], 0, stdout=pool.model_dump_json(), stderr=""
        )

    monkeypatch.setattr(refill_mod, "_run", fake_run)
    result = refill_mod.host_pool(ov, ov.hosts[0])
    assert result["a"]["label"] == "a"
    assert result["a"]["fingerprint"] == "fp-a"
    assert result["a"]["revoked"] is False
    assert result["a"]["limited"] is False
    assert result["b"]["label"] == "b"
    assert result["b"]["fingerprint"] == "fp-b"
    assert result["b"]["revoked"] is True
    assert result["b"]["limited"] is False


def test_host_pool_refuses_a_status_view_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ov = _overlay(tmp_path)
    view = ModelPoolStatusViewResult(
        host="h1",
        basis="local_pool",
        marks_source_host=None,
        marks_age_seconds=None,
        local_copy_age_seconds=None,
        tokens=2,
        labels=("a", "b"),
        limited=("b",),
        revoked=("b",),
        challenged=(),
        status_line="ok",
    )

    def fake_run(host: Any, script: str, *, timeout: float = 300) -> Any:
        return subprocess.CompletedProcess(
            [], 0, stdout=view.model_dump_json(), stderr=""
        )

    monkeypatch.setattr(refill_mod, "_run", fake_run)
    with pytest.raises(
        refill_mod.ClaudePoolRefillRefusedError,
        match="h1: the pool status carries no token entries",
    ):
        refill_mod.host_pool(ov, ov.hosts[0])


def test_handler_refuses_an_unsafe_roots_overlay(tmp_path: Path) -> None:
    lab = tmp_path / refill_mod.OVERLAY_REL.parent
    lab.mkdir(parents=True)
    (tmp_path / "hosts.yaml").write_text(
        "hosts:\n  - {name: hr, target: reader}\n  - {name: h1, target: one}\n",
        encoding="utf-8",
    )
    path = lab / refill_mod.OVERLAY_REL.name
    path.write_text(_OVERLAY + "\noverlay_roots: [/abs/path]\n", encoding="utf-8")
    refused = refill_mod.HandlerClaudePoolRefill().handle(
        ModelClaudePoolRefillRequest(config_path=str(path), check_config=True)
    )
    assert refused.exit_code == 2
    assert refused.refusal is not None
    assert "/abs/path" in refused.refusal


def test_handler_check_config_accepts_the_roots_overlay(tmp_path: Path) -> None:
    lab = tmp_path / refill_mod.OVERLAY_REL.parent
    lab.mkdir(parents=True)
    (tmp_path / "hosts.yaml").write_text(
        "hosts:\n  - {name: hr, target: reader}\n  - {name: h1, target: one}\n",
        encoding="utf-8",
    )
    path = lab / refill_mod.OVERLAY_REL.name
    path.write_text(
        _OVERLAY + "\noverlay_roots: [overlays/overlays]\n", encoding="utf-8"
    )
    ok = refill_mod.HandlerClaudePoolRefill().handle(
        ModelClaudePoolRefillRequest(config_path=str(path), check_config=True)
    )
    assert ok.exit_code == 0
    assert ok.lines[0].startswith("config ok: 1 store keys")


def test_overlay_path_prefers_the_overlay_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = tmp_path / "roots" / "node_claude_pool_refill_effect"
    roots.mkdir(parents=True)
    (roots / "overlay.yaml").write_text("store: {}\n", encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "roots"))
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(tmp_path / "config"))
    assert refill_mod.overlay_path(None) == roots / "overlay.yaml"


def test_overlay_path_falls_back_to_the_workspace_config_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(tmp_path))
    assert refill_mod.overlay_path(None) == tmp_path / refill_mod.OVERLAY_REL


def test_overlay_path_explicit_config_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "roots"))
    assert refill_mod.overlay_path("x.yaml") == Path("x.yaml")


def test_overlay_model_accepts_the_test_overlay() -> None:
    ModelClaudePoolRefillOverlay.model_validate(yaml.safe_load(_OVERLAY))


def test_overlay_model_refuses_an_unknown_key() -> None:
    with pytest.raises(ValidationError):
        ModelClaudePoolRefillOverlay.model_validate(
            yaml.safe_load(_OVERLAY + "\nsurprise: 1\n")
        )


def test_load_overlay_refuses_an_unknown_key_with_the_typed_refusal(
    tmp_path: Path,
) -> None:
    lab = tmp_path / refill_mod.OVERLAY_REL.parent
    lab.mkdir(parents=True)
    (tmp_path / "hosts.yaml").write_text(
        "hosts:\n  - {name: hr, target: reader}\n  - {name: h1, target: one}\n",
        encoding="utf-8",
    )
    path = lab / refill_mod.OVERLAY_REL.name
    path.write_text(_OVERLAY + "\nsurprise: 1\n", encoding="utf-8")
    with pytest.raises(
        refill_mod.ClaudePoolRefillRefusedError, match=re.escape(str(path))
    ):
        refill_mod.load_overlay(path)
