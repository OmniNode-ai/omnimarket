# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_tech_debt_sweep_orchestrator.

WS-5 Wave 8 (OMN-13682). The node's handler is a synchronous, deterministic
``handle(request) -> result`` over a repo scan (it is *not* an async
bus-consumer). The faithful integration test drives the handler in-process
against a *synthetic omni_home tree* and injects the Linear and stale-ignore
boundaries via mock adapters (the ``_Mock*`` collaborator pattern). NO
subprocess/asyncpg is monkeypatched; the I/O boundary is the injected adapter.

Each case varies the repo set, category scope, dry-run flag, dedup pre-seeding,
and stale-ignore adapter presence, and asserts the typed
``ModelTechDebtSweepResult`` (finding/new/ticket counts, per-category results,
skipped-stale repos, dry-run flag).

Negative control: the ``todo_repo`` fixture contains a known ``# TODO`` marker
that MUST surface as exactly one finding; the ``clean_repo`` must surface zero.
A scan that reported zero findings over ``todo_repo`` would be a regression.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_tech_debt_sweep_orchestrator.handlers.handler_tech_debt_sweep_orchestrator import (
    HandlerTechDebtSweepOrchestrator,
)
from omnimarket.nodes.node_tech_debt_sweep_orchestrator.models.model_tech_debt_sweep_request import (
    ModelTechDebtSweepRequest,
)

# The exact TODO line seeded into todo_repo/mod.py (line 1).
_TODO_LINE = "x = 1  # TODO: refactor"


def _dedup_key(
    category: str, repo: str, relative_path: str, line_number: int, line_text: str
) -> str:
    """Mirror HandlerTechDebtSweepOrchestrator._finding dedup-key derivation."""
    basis = "\n".join(
        [category, repo, relative_path, str(line_number), " ".join(line_text.split())]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


_TODO_KEY = _dedup_key("todo-fixme", "todo_repo", "mod.py", 1, _TODO_LINE)


class _MockLinear:
    """Injected Linear boundary; records epic/ticket creation, replays dedup keys."""

    def __init__(self, dedup_keys: set[str] | None = None) -> None:
        self._dedup_keys = dedup_keys or set()
        self.epics: list[dict[str, Any]] = []
        self.tickets: list[dict[str, Any]] = []

    def open_dedup_keys(self) -> set[str]:
        return set(self._dedup_keys)

    def create_epic(self, payload: dict[str, Any]) -> str:
        self.epics.append(payload)
        return f"EPIC-{len(self.epics)}"

    def create_ticket(self, payload: dict[str, Any]) -> str:
        self.tickets.append(payload)
        return f"OMN-{len(self.tickets)}"


def _build_omni_home(tmp_path: Path) -> Path:
    omni_home = tmp_path / "omni_home"
    todo_repo = omni_home / "todo_repo"
    clean_repo = omni_home / "clean_repo"
    todo_repo.mkdir(parents=True, exist_ok=True)
    clean_repo.mkdir(parents=True, exist_ok=True)
    (todo_repo / "pyproject.toml").write_text(
        '[project]\nname = "todo_repo"\n', encoding="utf-8"
    )
    (clean_repo / "pyproject.toml").write_text(
        '[project]\nname = "clean_repo"\n', encoding="utf-8"
    )
    (todo_repo / "mod.py").write_text(f"{_TODO_LINE}\n", encoding="utf-8")
    (clean_repo / "mod.py").write_text("y = 2\n", encoding="utf-8")
    return omni_home


@dataclass(frozen=True)
class _Case:
    id: str
    repos: tuple[str, ...]
    categories: tuple[str, ...]
    dry_run: bool
    provide_linear: bool
    seed_dedup: bool
    provide_stale: bool
    expected_total: int
    expected_new: int
    expected_tickets: int
    expected_skipped_stale: tuple[str, ...] = field(default=())


_CASES = [
    _Case(
        id="todo-dry-run-finding",
        repos=("todo_repo",),
        categories=("todo-fixme",),
        dry_run=True,
        provide_linear=False,
        seed_dedup=False,
        provide_stale=False,
        expected_total=1,
        expected_new=1,
        expected_tickets=0,
    ),
    _Case(
        id="clean-repo-no-findings",
        repos=("clean_repo",),
        categories=("todo-fixme",),
        dry_run=True,
        provide_linear=False,
        seed_dedup=False,
        provide_stale=False,
        expected_total=0,
        expected_new=0,
        expected_tickets=0,
    ),
    _Case(
        id="todo-live-creates-ticket",
        repos=("todo_repo",),
        categories=("todo-fixme",),
        dry_run=False,
        provide_linear=True,
        seed_dedup=False,
        provide_stale=False,
        expected_total=1,
        expected_new=1,
        expected_tickets=1,
    ),
    _Case(
        id="todo-dedup-against-existing-ticket",
        repos=("todo_repo",),
        categories=("todo-fixme",),
        dry_run=False,
        provide_linear=True,
        seed_dedup=True,
        provide_stale=False,
        expected_total=1,
        expected_new=0,
        expected_tickets=0,
    ),
    _Case(
        id="stale-ignores-without-adapter-skips-repo",
        repos=("todo_repo",),
        categories=("stale-ignores",),
        dry_run=True,
        provide_linear=False,
        seed_dedup=False,
        provide_stale=False,
        expected_total=0,
        expected_new=0,
        expected_tickets=0,
        expected_skipped_stale=("todo_repo",),
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize("case", _CASES, ids=[c.id for c in _CASES])
def test_tech_debt_sweep_multiparam(tmp_path: Path, case: _Case) -> None:
    omni_home = _build_omni_home(tmp_path)
    linear = (
        _MockLinear({_TODO_KEY} if case.seed_dedup else set())
        if case.provide_linear
        else None
    )
    handler = HandlerTechDebtSweepOrchestrator(linear_adapter=linear)

    result = handler.handle(
        ModelTechDebtSweepRequest(
            repos=case.repos,
            categories=case.categories,
            dry_run=case.dry_run,
            omni_home=str(omni_home),
        )
    )

    assert result.total_findings == case.expected_total
    assert result.total_new_findings == case.expected_new
    assert result.total_tickets_created == case.expected_tickets
    assert result.repos_skipped_stale_ignores == case.expected_skipped_stale
    assert result.dry_run is case.dry_run
    assert set(result.repos_scanned) == set(case.repos)
    # Per-category results always cover the requested categories.
    assert {r.category for r in result.category_results} == set(case.categories)

    if case.expected_tickets:
        assert linear is not None
        assert len(linear.tickets) == case.expected_tickets
        assert len(linear.epics) >= 1
    if case.seed_dedup:
        todo_result = next(
            r for r in result.category_results if r.category == "todo-fixme"
        )
        assert todo_result.already_tracked == 1


@pytest.mark.integration
def test_tech_debt_stale_ignores_uses_injected_adapter(tmp_path: Path) -> None:
    """When a stale-ignore adapter is injected its findings flow into the result."""
    omni_home = _build_omni_home(tmp_path)

    class _MockStale:
        def find_stale_type_ignores(self, repo_path: Path) -> list[dict[str, Any]]:
            return [
                {
                    "path": str(repo_path / "mod.py"),
                    "line_number": 1,
                    "line_text": "stale: unused ignore",
                }
            ]

    result = HandlerTechDebtSweepOrchestrator(
        linear_adapter=_MockLinear(),
        stale_ignore_adapter=_MockStale(),
    ).handle(
        ModelTechDebtSweepRequest(
            repos=("todo_repo",),
            categories=("stale-ignores",),
            dry_run=True,
            omni_home=str(omni_home),
        )
    )

    assert result.repos_skipped_stale_ignores == ()
    stale = next(r for r in result.category_results if r.category == "stale-ignores")
    assert stale.total_findings == 1
