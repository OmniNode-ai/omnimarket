# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A label a host holds and the store lacks is adopted into the store (OMN-23895).

The remote calls (pool status, the export-to-seed pipe) are fakes over fingerprints, so the tests hold the decisions:
which labels are adopted, that a different store value is never overwritten, that hosts that disagree adopt nothing,
that a failed seed is reported and not counted as adopted, that a dry run only plans, and that an overlay without the
adopt block refuses. No value exists in any of these tests: fingerprints only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_claude_pool_refill_effect.handlers import (
    handler_claude_pool_refill as refill_mod,
)
from omnimarket.nodes.node_claude_pool_refill_effect.models.model_claude_pool_refill import (
    ModelClaudePoolRefillRequest,
)

pytestmark = pytest.mark.unit

OVERLAY = """
store:
  project_id: 00000000-0000-0000-0000-000000000000
  environment: dev
  folder: /shared/pool
  reader_host: hr
  identity_env_file: ~/.id.env
hosts_table: hosts.yaml
tokens:
  - {label: a, key: K_A}
hosts: [h1, h2]
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
ADOPT_BLOCK = "adopt:\n  key_prefix: K_\n"


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ONEX_LAB_RUN_HOSTS",
        "ONEX_WORKSPACE_CONFIG_ROOT",
        "ONEX_SKILL_OVERLAY_ROOTS",
    ):
        monkeypatch.delenv(name, raising=False)


def overlay(tmp_path: Path, *, block: bool = True) -> Any:
    lab = tmp_path / refill_mod.OVERLAY_REL.parent
    lab.mkdir(parents=True)
    (tmp_path / "hosts.yaml").write_text(
        "hosts:\n  - {name: hr, target: reader}\n  - {name: h1, target: one}\n  - {name: h2, target: two}\n",
        encoding="utf-8",
    )
    path = lab / refill_mod.OVERLAY_REL.name
    path.write_text(OVERLAY + (ADOPT_BLOCK if block else ""), encoding="utf-8")
    return refill_mod.load_overlay(path)


def entry(label: str, fp: str, revoked: bool = False) -> dict[str, Any]:
    return {"label": label, "fingerprint": fp, "revoked": revoked, "limited": False}


class Fakes:
    def __init__(
        self, pools: dict[str, dict[str, dict[str, Any]]], seed: str = "ok"
    ) -> None:
        self.pools = pools
        self.seed = seed
        self.piped: list[tuple[str, str, str, bool]] = []

    def host_pool(self, ov: Any, host: Any) -> dict[str, dict[str, Any]]:
        return self.pools[host.name]

    def adopt(self, ov: Any, source: Any, command: Any, *, execute: bool) -> str:
        self.piped.append((source.name, command.label, command.key, execute))
        if self.seed != "ok":
            return f"FAILED seed {command.label}: verdict={self.seed} rc=1"
        word = "adopted" if execute else "would-adopt"
        return f"{word} {command.label} key={command.key} verdict={'seeded' if execute else 'dry_run'}"


def rig(monkeypatch: pytest.MonkeyPatch, fakes: Fakes) -> None:
    monkeypatch.setattr(refill_mod, "host_pool", fakes.host_pool)
    monkeypatch.setattr(refill_mod, "adopt", fakes.adopt)


def served(ov: Any) -> list[Any]:
    return list(ov.hosts)


def test_a_live_label_outside_the_store_is_adopted_and_joins_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ov = overlay(tmp_path)
    fakes = Fakes(
        {
            "h1": {"a": entry("a", "fa"), "onexbot": entry("onexbot", "4d2430e0")},
            "h2": {"a": entry("a", "fa"), "onexbot": entry("onexbot", "4d2430e0")},
        }
    )
    rig(monkeypatch, fakes)
    store = {"a": "fa"}
    lines = refill_mod.adopt_held_labels(ov, served(ov), store, execute=True)
    assert fakes.piped == [("h1", "onexbot", "K_ONEXBOT", True)]
    assert [t.label for t in ov.tokens] == ["a", "onexbot"]
    assert store["onexbot"] == "4d2430e0"
    assert any("overlay-add label=onexbot key=K_ONEXBOT" in line for line in lines)


def test_control_a_store_value_that_differs_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ov = overlay(tmp_path)
    fakes = Fakes({"h1": {"a": entry("a", "other")}, "h2": {"a": entry("a", "other")}})
    rig(monkeypatch, fakes)
    lines = refill_mod.adopt_held_labels(ov, served(ov), {"a": "fa"}, execute=True)
    assert fakes.piped == []
    assert lines == ["claude-pool-adopt label=a store_differs"]


def test_control_hosts_that_disagree_adopt_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ov = overlay(tmp_path)
    fakes = Fakes({"h1": {"x": entry("x", "f1")}, "h2": {"x": entry("x", "f2")}})
    rig(monkeypatch, fakes)
    lines = refill_mod.adopt_held_labels(ov, served(ov), {"a": "none"}, execute=True)
    assert fakes.piped == []
    assert lines == ["claude-pool-adopt label=x conflicting_fingerprints"]


def test_control_a_failed_seed_is_reported_and_the_label_does_not_join_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ov = overlay(tmp_path)
    fakes = Fakes({"h1": {"x": entry("x", "f1")}, "h2": {}}, seed="auth_unavailable")
    rig(monkeypatch, fakes)
    store = {"a": "none"}
    lines = refill_mod.adopt_held_labels(ov, served(ov), store, execute=True)
    assert any("FAILED" in line for line in lines)
    assert [t.label for t in ov.tokens] == ["a"]
    assert "x" not in store


def test_a_dry_run_only_plans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ov = overlay(tmp_path)
    fakes = Fakes({"h1": {"x": entry("x", "f1")}, "h2": {}})
    rig(monkeypatch, fakes)
    store = {"a": "none"}
    lines = refill_mod.adopt_held_labels(ov, served(ov), store, execute=False)
    assert fakes.piped == [("h1", "x", "K_X", False)]
    assert any("would-adopt" in line for line in lines)
    assert store["x"] == "none"


def test_control_a_label_revoked_everywhere_is_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ov = overlay(tmp_path)
    fakes = Fakes(
        {"h1": {"x": entry("x", "f1", True)}, "h2": {"x": entry("x", "f1", True)}}
    )
    rig(monkeypatch, fakes)
    refill_mod.adopt_held_labels(ov, served(ov), {"a": "none"}, execute=True)
    assert fakes.piped == []


def test_an_overlay_without_the_adopt_block_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ov = overlay(tmp_path, block=False)
    rig(monkeypatch, Fakes({}))
    with pytest.raises(refill_mod.ClaudePoolRefillRefusedError, match="adopt"):
        refill_mod.adopt_held_labels(ov, served(ov), {}, execute=True)


def test_the_handler_runs_adopt_before_the_refill_and_fails_the_run_on_a_failed_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ov = overlay(tmp_path)
    fakes = Fakes({"h1": {"x": entry("x", "f1")}, "h2": {}}, seed="auth_unavailable")
    rig(monkeypatch, fakes)
    monkeypatch.setattr(refill_mod, "load_overlay", lambda _path: ov)
    monkeypatch.setattr(refill_mod, "store_fingerprints", lambda _ov: {"a": "none"})
    monkeypatch.setattr(
        refill_mod,
        "refill",
        lambda _ov, host, _store, **_kw: f"refill {host.name} verdict=ok",
    )
    result = refill_mod.HandlerClaudePoolRefill().handle(
        ModelClaudePoolRefillRequest(adopt=True)
    )
    assert result.exit_code == 1
    assert any("FAILED" in line for line in result.lines)
    assert [line for line in result.lines if line.startswith("refill ")]


def test_the_seed_script_pipes_stdin_and_adds_execute_only_when_asked(
    tmp_path: Path,
) -> None:
    ov = overlay(tmp_path)
    plan = refill_mod._seed_script(ov, "K_X", execute=False)
    assert "--source-path -" in plan
    assert "--execute" not in plan
    assert "--execute" in refill_mod._seed_script(ov, "K_X", execute=True)
