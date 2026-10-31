# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_claude_pool_refill_effect (OMN-21626).

The remote calls (pool status, store read, add, probe, mark) are replaced with fakes, so the tests hold
the decisions: which store tokens a host is missing, how an answer is classified from the overlay's
patterns, that a refused or expired label is marked and the next one probed, that a usage limit is
marked on the account across hosts, that every host draws its own first token, and that the overlay is
the only source of hosts, keys and patterns.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_claude_pool_refill_effect.handlers import (
    handler_claude_pool_refill as refill_mod,
)
from omnimarket.nodes.node_claude_pool_refill_effect.models.model_claude_pool_refill import (
    ModelClaudePoolRefillRequest,
)

pytestmark = pytest.mark.unit

_SCRIPT = Path(refill_mod.__file__)
_PRIVATE_IPV4 = re.compile(
    r"\b(?:10\.\d+|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d+\.\d+"
)
_NODE = _SCRIPT.parents[1]

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
  - {label: b, key: K_B}
  - {label: c, key: K_C}
hosts: [h1, h2, h3]
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


def _overlay(tmp_path: Path) -> Any:
    lab = tmp_path / refill_mod.OVERLAY_REL.parent
    lab.mkdir(parents=True)
    (tmp_path / "hosts.yaml").write_text(
        "hosts:\n  - {name: hr, target: reader}\n  - {name: h1, target: one}\n"
        "  - {name: h2, target: two}\n  - {name: h3, target: three}\n",
        encoding="utf-8",
    )
    path = lab / refill_mod.OVERLAY_REL.name
    path.write_text(_OVERLAY, encoding="utf-8")
    return refill_mod.load_overlay(path)


def test_missing_overlay_is_refused_naming_its_path(tmp_path: Path) -> None:
    with pytest.raises(
        refill_mod.ClaudePoolRefillRefusedError, match=str(tmp_path / "nope.yaml")
    ):
        refill_mod.load_overlay(tmp_path / "nope.yaml")


def test_overlay_names_every_host_key_and_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LAB_RUN_HOSTS", raising=False)
    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)
    ov = _overlay(tmp_path)
    assert [t.key for t in ov.tokens] == ["K_A", "K_B", "K_C"]
    assert ov.reader.target == "reader"
    assert [h.target for h in ov.hosts] == ["one", "two", "three"]
    assert (
        refill_mod.classify(ov, "Your organization has disabled Claude", 1) == "refused"
    )
    assert refill_mod.classify(ov, "Failed: OAuth token revoked.", 1) == "expired"
    assert refill_mod.classify(ov, "You've hit your weekly limit", 1) == "limited"
    assert refill_mod.classify(ov, "ok", 0) == "ok"
    assert refill_mod.classify(ov, "boom", 2) == "unknown-rc2"


def test_a_host_not_in_the_hosts_table_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LAB_RUN_HOSTS", raising=False)
    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)
    _overlay(tmp_path)
    path = tmp_path / refill_mod.OVERLAY_REL
    path.write_text(
        _OVERLAY.replace("hosts: [h1, h2, h3]", "hosts: [h9]"), encoding="utf-8"
    )
    with pytest.raises(refill_mod.ClaudePoolRefillRefusedError, match="h9"):
        refill_mod.load_overlay(path)


class _Fakes:
    def __init__(
        self, pool: dict[str, dict[str, Any]], answers: dict[str, str]
    ) -> None:
        self.pool = pool
        self.answers = answers
        self.installed: list[tuple[str, bool]] = []
        self.marked: list[tuple[str, str]] = []
        self.ordered: list[list[str]] = []

    def host_pool(self, ov: Any, host: Any) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self.pool.items()}

    def install(self, ov: Any, host: Any, token: Any, *, force: bool) -> str:
        self.installed.append((token.label, force))
        self.pool[token.label] = {
            "label": token.label,
            "fingerprint": "new",
            "revoked": False,
            "limited": False,
        }
        return f"claude-pool added label={token.label}"

    def probe(self, ov: Any, host: Any) -> tuple[str | None, str, str]:
        for label, entry in self.pool.items():
            if not entry["revoked"] and not entry["limited"]:
                verdict = self.answers.get(label, "ok")
                return label, verdict, verdict
        return None, "empty", ""

    def mark(self, ov: Any, host: Any, label: str, verdict: str) -> None:
        self.marked.append((label, verdict))
        self.pool[label]["limited" if verdict == "limited" else "revoked"] = True

    def order_pool(self, ov: Any, host: Any, labels: list[str]) -> str:
        self.ordered.append(labels)
        rest = [k for k in self.pool if k not in labels]
        self.pool = {k: self.pool[k] for k in [*labels, *rest]}
        return ""


def _entry(label: str, fp: str) -> dict[str, Any]:
    return {"label": label, "fingerprint": fp, "revoked": False, "limited": False}


def test_refused_then_expired_labels_are_marked_and_the_next_live_one_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LAB_RUN_HOSTS", raising=False)
    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)
    ov = _overlay(tmp_path)
    fakes = _Fakes(
        {"a": _entry("a", "fa"), "b": _entry("b", "fb")},
        {"a": "refused", "b": "expired"},
    )
    for name in ("host_pool", "install", "probe", "mark", "order_pool"):
        monkeypatch.setattr(refill_mod, name, getattr(fakes, name))
    line = refill_mod.refill(
        ov, ov.hosts[0], {"a": "fa", "b": "fb", "c": "fc"}, dry_run=False
    )
    assert fakes.installed == [("c", False)]
    assert fakes.marked == [("a", "refused"), ("b", "expired")]
    assert fakes.ordered == []  # a, b, c is already the first host's draw order
    assert "first=a" in line
    assert "now=c" in line
    assert line.endswith("verdict=ok")


def test_a_changed_store_value_replaces_the_label_and_a_known_value_is_not_copied_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LAB_RUN_HOSTS", raising=False)
    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)
    ov = _overlay(tmp_path)
    fakes = _Fakes({"a": _entry("a", "old"), "other": _entry("other", "fc")}, {})
    for name in ("host_pool", "install", "probe", "mark", "order_pool"):
        monkeypatch.setattr(refill_mod, name, getattr(fakes, name))
    refill_mod.refill(
        ov, ov.hosts[0], {"a": "fa", "b": "none", "c": "fc"}, dry_run=False
    )
    assert fakes.installed == [("a", True)]


def test_dry_run_adds_and_marks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LAB_RUN_HOSTS", raising=False)
    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)
    ov = _overlay(tmp_path)
    fakes = _Fakes({"a": _entry("a", "fa")}, {"a": "refused"})
    for name in ("host_pool", "install", "probe", "mark", "order_pool"):
        monkeypatch.setattr(refill_mod, name, getattr(fakes, name))
    line = refill_mod.refill(ov, ov.hosts[0], {"a": "fa", "b": "fb"}, dry_run=True)
    assert fakes.installed == []
    assert fakes.marked == []
    assert fakes.ordered == []
    assert "b(would-add)" in line
    assert "verdict=refused" in line


def test_the_script_names_no_host_address_model_or_store_path() -> None:
    text = _SCRIPT.read_text(encoding="utf-8")
    assert '"claude"' not in text
    assert "claude -p" not in text
    assert _PRIVATE_IPV4.search(text) is None
    for literal in ("/shared/", "sonnet", "opus", ".omnibase"):
        assert literal not in text, literal


def test_contract_names_the_handler_and_models() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    handler = contract["handler"]
    assert (
        contract["handler_routing"]["default_handler"]
        == f"{handler['module']}:{handler['class']}"
    )
    assert handler["module"] == refill_mod.__name__
    assert getattr(refill_mod, handler["class"]) is refill_mod.HandlerClaudePoolRefill
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}


def test_handler_check_config_and_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LAB_RUN_HOSTS", raising=False)
    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)
    _overlay(tmp_path)
    path = tmp_path / refill_mod.OVERLAY_REL
    ok = refill_mod.HandlerClaudePoolRefill().handle(
        ModelClaudePoolRefillRequest(config_path=str(path), check_config=True)
    )
    assert ok.exit_code == 0
    assert ok.lines[0].startswith("config ok: 3 store keys")
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(tmp_path / "absent"))
    refused = refill_mod.HandlerClaudePoolRefill().handle(
        ModelClaudePoolRefillRequest(check_config=True)
    )
    assert refused.exit_code == 2
    assert refused.refusal is not None
    assert str(tmp_path / "absent") in refused.refusal


def test_each_host_draws_a_different_first_token_from_the_overlay_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LAB_RUN_HOSTS", raising=False)
    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)
    ov = _overlay(tmp_path)
    orders = {h.name: [t.label for t in refill_mod.draw_order(ov, h)] for h in ov.hosts}
    assert orders == {
        "h1": ["a", "b", "c"],
        "h2": ["b", "c", "a"],
        "h3": ["c", "a", "b"],
    }
    assert len({o[0] for o in orders.values()}) == len(ov.hosts)
    assert orders == {
        h.name: [t.label for t in refill_mod.draw_order(ov, h)] for h in ov.hosts
    }


def test_an_established_pool_is_reordered_to_the_hosts_draw_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LAB_RUN_HOSTS", raising=False)
    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)
    ov = _overlay(tmp_path)
    fakes = _Fakes(
        {"x": _entry("x", "fx"), "a": _entry("a", "fa"), "b": _entry("b", "fb")}, {}
    )
    for name in ("host_pool", "install", "probe", "mark", "order_pool"):
        monkeypatch.setattr(refill_mod, name, getattr(fakes, name))
    line = refill_mod.refill(
        ov, ov.hosts[1], {"a": "fa", "b": "fb", "c": "none"}, dry_run=False
    )
    assert fakes.ordered == [["b", "a"]]
    assert "order=reordered:b" in line
    assert list(fakes.pool) == ["b", "a", "x"]


def test_a_usage_limit_is_marked_on_the_account_across_every_host_that_holds_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LAB_RUN_HOSTS", raising=False)
    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)
    ov = _overlay(tmp_path)
    pools = {
        "h1": _Fakes(
            {"a": _entry("a", "fa"), "b": _entry("b", "fb")}, {"a": "limited"}
        ),
        "h2": _Fakes({"b": _entry("b", "fb"), "a": _entry("a", "fa")}, {}),
        "h3": _Fakes({"a": _entry("a", "other"), "b": _entry("b", "fb")}, {}),
    }
    probed: list[str] = []

    def per_host(name: str) -> Any:
        def call(fn: str) -> Any:
            def inner(_ov: Any, host: Any, *args: Any, **kwargs: Any) -> Any:
                fake = pools[host.name]
                if fn == "probe":
                    probed.append(host.name)
                return getattr(fake, fn)(_ov, host, *args, **kwargs)

            return inner

        return call(name)

    for name in ("host_pool", "install", "probe", "mark", "order_pool"):
        monkeypatch.setattr(refill_mod, name, per_host(name))
    store = {"a": "fa", "b": "fb", "c": "none"}
    state = refill_mod.RunState(hosts=list(ov.hosts))
    lines = [
        refill_mod.refill(ov, h, store, dry_run=False, state=state) for h in ov.hosts
    ]
    assert pools["h1"].marked == [("a", "limited")]
    assert (
        "limit_spread=h2:a" not in lines[0]
    )  # h2 had not run yet; it takes the mark itself
    assert pools["h2"].marked == [("a", "limited")]
    assert "limit_spread=h2:a" in lines[1]
    assert (
        pools["h3"].marked == []
    )  # h3 holds another value under that label: another account
    assert all(line.endswith("verdict=ok") for line in lines)
    assert probed == [
        "h1",
        "h1",
        "h2",
        "h3",
    ]  # h2 and h3 spent no probe finding the limit


def test_probe_goes_through_the_delegation_node_and_reads_its_typed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LAB_RUN_HOSTS", raising=False)
    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)
    ov = _overlay(tmp_path)
    seen: list[str] = []

    class _Done:
        returncode = 0

        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def answer(receipt: dict[str, Any]) -> Any:
        def fake(host: Any, script: str, *, timeout: float = 300) -> Any:
            seen.append(script)
            return _Done("LABEL=a\n" + json.dumps(receipt) + "\n")

        return fake

    ok = {
        "output": "ok",
        "wall_seconds": 1.0,
        "model": "m",
        "exit_cause": "answered",
        "exit_code": 0,
    }
    monkeypatch.setattr(refill_mod, "_run", answer(ok))
    assert refill_mod.probe(ov, ov.hosts[0]) == ("a", "ok", "ok")
    script = seen[0]
    assert refill_mod.PROBE_MODULE in script
    assert "claude -p" not in script
    assert "$CP claude" not in script
    assert '"prompt":"say ok"' in script

    limited = ok | {
        "output": "You've hit your weekly limit",
        "exit_cause": "process_failed",
        "exit_code": 1,
    }
    monkeypatch.setattr(refill_mod, "_run", answer(limited))
    assert refill_mod.probe(ov, ov.hosts[0])[1] == "limited"

    timeout = ok | {"output": "", "exit_cause": "timeout", "exit_code": -9}
    monkeypatch.setattr(refill_mod, "_run", answer(timeout))
    assert refill_mod.probe(ov, ov.hosts[0])[1] == "unknown-rc124"

    def no_receipt(host: Any, script: str, *, timeout: float = 300) -> Any:
        return _Done("LABEL=a\nNo module named 'x'\n")

    monkeypatch.setattr(refill_mod, "_run", no_receipt)
    assert refill_mod.probe(ov, ov.hosts[0])[1] == "probe-unavailable"
