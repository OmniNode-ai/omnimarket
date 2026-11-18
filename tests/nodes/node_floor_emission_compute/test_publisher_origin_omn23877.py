# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every publisher of a chain event says what kind of work the chain is (OMN-23877).

The readiness rule counts a join only when its chains came from real work. A publisher that sets no origin reads ``unknown``,
and the board then shows every piece as green on probes only, so each publisher's origin is a decision made in its source and
read here. The table below is that decision, one row per source file: the origins its emission sites name. A new emission
site with no origin, a probe that says ``real`` and a real unit of work that says anything else each fail one test.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src" / "omnimarket"
NODES = SRC / "nodes"

# The factory's own units of work: the landing controller's steps, the live J8 run and the host drain. Everything else that
# publishes a chain is a probe, a canary, a control or a drill.
REAL = {
    "node_pr_landing_tick_orchestrator/handlers/handler_pr_landing_tick_orchestrator.py": {
        "real"
    },
    "node_pr_landing_snapshot_effect/runtime_snapshot.py": {"real"},
    "node_pr_landing_watcher_facts_effect/runtime_watcher_facts.py": {"real"},
    "node_lane_host_pool_snapshot_effect/runtime_pool_snapshot.py": {"real"},
    "node_pr_landing_decision_compute/handlers/handler_landing_join_j8_live.py": {
        "real"
    },
    "node_integration_reading_publish_effect/runtime_integration_reading_publish.py": {
        "real"
    },
    "node_host_drain_effect/handlers/handler_host_drain.py": {"real"},
    "node_host_drain_effect/handlers/handler_idle_runner_stop.py": {"real"},
}


def _literals(origin: ast.expr | None) -> set[str]:
    """Every string constant an origin expression can take (``"drill" if flag else "probe"`` names two)."""
    if origin is None:
        return set()
    return {
        str(n.value)
        for n in ast.walk(origin)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _is_site(node: ast.Call) -> str | None:
    func = node.func
    name = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute)
        else ""
    )
    if name == "ChainEmitter":
        return "chain"
    if name == "ModelNodeEmission":
        return "model"
    if name == "model_validate" and node.args and isinstance(node.args[0], ast.Dict):
        keys = {k.value for k in node.args[0].keys if isinstance(k, ast.Constant)}
        if {"node", "piece", "correlation_id", "phase"} <= keys:
            return "dict"
    return None


def _origin_of(node: ast.Call, kind: str) -> ast.expr | None:
    if kind == "dict":
        assert isinstance(node.args[0], ast.Dict)
        for key, value in zip(node.args[0].keys, node.args[0].values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "origin":
                return value
        return None
    for keyword in node.keywords:
        if keyword.arg == "origin":
            return keyword.value
    return None


def sites() -> list[tuple[str, int, ast.expr | None]]:
    found: list[tuple[str, int, ast.expr | None]] = []
    for path in sorted(NODES.rglob("*.py")):
        rel = path.relative_to(NODES).as_posix()
        if "/tests/" in rel or "node_floor_emission_compute/" in rel:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and (kind := _is_site(node)):
                found.append((rel, node.lineno, _origin_of(node, kind)))
    return found


def test_golden_the_scan_finds_the_publishers() -> None:
    files = {rel for rel, _, _ in sites()}
    assert set(REAL) <= files
    assert len(files) >= 25


def test_error_chain_every_emission_site_names_its_origin() -> None:
    unset = [f"{rel}:{line}" for rel, line, origin in sites() if origin is None]
    assert unset == [], "emission sites that set no origin: " + ", ".join(unset)


def test_a_real_unit_of_work_says_real() -> None:
    by_file: dict[str, set[str]] = {}
    for rel, _, origin in sites():
        by_file.setdefault(rel, set()).update(_literals(origin))
    for rel, expected in REAL.items():
        assert by_file.get(rel, set()) >= expected, rel


def test_named_break_nothing_else_says_real() -> None:
    liars = [
        f"{rel}:{line}"
        for rel, line, origin in sites()
        if rel not in REAL and "real" in _literals(origin)
    ]
    assert liars == [], "a probe, canary, control or drill says real: " + ", ".join(
        liars
    )


def test_named_break_an_unlisted_publisher_with_a_literal_unknown_is_still_a_decision() -> (
    None
):
    """``unknown`` is allowed only as a stated decision, and every use is visible here."""
    unknown = sorted(
        {rel for rel, _, origin in sites() if "unknown" in _literals(origin)}
    )
    assert unknown == []
