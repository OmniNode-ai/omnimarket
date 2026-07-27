# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Workflow module-resolution guard tests (OMN-14176).

Two responsibilities:

1. **Behavioural** — the guard resolves in-repo (``omnimarket.*``) module refs in
   ``.github/workflows/*.yml`` against ``src/omnimarket/``, catches a deleted
   module, and does NOT false-positive on sibling-repo refs cloned at CI runtime.
   Includes a fail-closed proof that the *live* tree has zero unresolved in-repo
   refs — the regression that node_pr_review_bot (deleted in OMN-13212) but still
   imported by pr-review-bot.yml + pr-arch-review.yml would have tripped.

2. **Wiring** — the guard is a real gate, not advisory (Operating Rule 5): a
   ``workflow-module-refs`` pre-commit hook exists, a ``workflow-module-refs`` CI
   job exists, and the required ``ci-summary`` poller treats its job name as a
   strict gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci.check_workflow_module_refs import (
    collect_workflow_refs,
    extract_module_refs,
    find_unresolved_in_repo_refs,
    is_in_repo,
    module_resolves,
)
from scripts.ci.ci_summary_gate import STRICT_GATE_JOBS

REPO_ROOT = Path(__file__).parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PRE_COMMIT = REPO_ROOT / ".pre-commit-config.yaml"

DEAD_MODULE = "omnimarket.nodes.node_pr_review_bot.workflow_runner"
SIBLING_REF = "omniintelligence.review_pairing.cli_review"


@pytest.mark.unit
class TestClassification:
    def test_omnimarket_is_in_repo(self) -> None:
        assert is_in_repo("omnimarket")
        assert is_in_repo("omnimarket.nodes.node_runtime_sweep")

    def test_sibling_is_not_in_repo(self) -> None:
        assert not is_in_repo(SIBLING_REF)
        assert not is_in_repo("omnibase_core.validation.validator_url_authority")

    def test_prefix_lookalike_is_not_in_repo(self) -> None:
        # A package that merely starts with "omnimarket" text but is a distinct
        # top-level name must not be misclassified. There is no dotted boundary,
        # so "omnimarketing" is not the omnimarket package.
        assert not is_in_repo("omnimarketing.foo")


@pytest.mark.unit
class TestModuleResolution:
    def test_resolves_module_file_and_package(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        (src / "omnimarket" / "sub").mkdir(parents=True)
        (src / "omnimarket" / "__init__.py").write_text("")
        (src / "omnimarket" / "leaf.py").write_text("")
        (src / "omnimarket" / "sub" / "__init__.py").write_text("")

        assert module_resolves("omnimarket", src_root=src)
        assert module_resolves("omnimarket.leaf", src_root=src)
        assert module_resolves("omnimarket.sub", src_root=src)
        assert not module_resolves("omnimarket.missing", src_root=src)
        assert not module_resolves("omnimarket.sub.gone", src_root=src)

    def test_live_repo_known_modules_resolve(self) -> None:
        # Modules that live workflows genuinely invoke must resolve.
        assert module_resolves("omnimarket.runtime.version_handshake")
        assert module_resolves("omnimarket.validators.event_registry_drift")
        assert module_resolves("omnimarket.nodes.node_runtime_sweep")

    def test_deleted_node_does_not_resolve(self) -> None:
        assert not module_resolves(DEAD_MODULE)
        assert not module_resolves("omnimarket.nodes.node_pr_review_bot")


@pytest.mark.unit
class TestExtraction:
    def test_extracts_from_import(self) -> None:
        refs = extract_module_refs(f"          from {DEAD_MODULE} import run_review")
        assert [(n, m) for n, m, _ in refs] == [(1, DEAD_MODULE)]

    def test_extracts_dash_m(self) -> None:
        refs = extract_module_refs(
            "        run: uv run python -m omnimarket.nodes.node_runtime_sweep --import-check"
        )
        assert ("omnimarket.nodes.node_runtime_sweep") in {m for _, m, _ in refs}

    def test_prose_from_is_not_matched(self) -> None:
        # "from omnimarket CI" in an English comment must not be read as an import:
        # the regex requires the `import` keyword immediately after the module.
        prose = "  # aislop detection was ABSENT from omnimarket CI; this job makes it required"
        assert extract_module_refs(prose) == []

    def test_history_prose_module_mention_is_not_matched(self) -> None:
        # A bare dotted mention in prose (no from/import/-m surface) is not a ref.
        prose = "# the node_pr_review_bot.workflow_runner module tree was deleted in OMN-13212"
        assert extract_module_refs(prose) == []


@pytest.mark.unit
class TestGuardBehaviour:
    def test_live_tree_has_no_unresolved_in_repo_refs(self) -> None:
        """Fail-closed proof: every in-repo module a workflow imports resolves."""
        unresolved = find_unresolved_in_repo_refs()
        assert unresolved == [], (
            "Workflows reference omnimarket modules that do not exist under "
            "src/omnimarket/:\n"
            + "\n".join(f"  {r.workflow}:{r.line_no}: {r.module}" for r in unresolved)
        )

    def test_guard_catches_a_planted_deleted_module(self, tmp_path: Path) -> None:
        """The guard must flag a workflow importing a nonexistent in-repo module."""
        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "planted.yml").write_text(
            "jobs:\n  x:\n    steps:\n      - run: |\n"
            f"          from {DEAD_MODULE} import run_review\n"
        )
        src = tmp_path / "src"
        (src / "omnimarket").mkdir(parents=True)
        (src / "omnimarket" / "__init__.py").write_text("")

        unresolved = find_unresolved_in_repo_refs(workflows_dir=workflows, src_root=src)
        assert [r.module for r in unresolved] == [DEAD_MODULE]

    def test_sibling_repo_refs_are_out_of_scope(self) -> None:
        """Cross-repo refs (cloned at CI runtime) are seen but never failed."""
        refs = collect_workflow_refs()
        modules = {r.module for r in refs}
        # hostile-reviewer.yml legitimately runs a sibling-clone module.
        assert SIBLING_REF in modules, "expected the sibling ref to be discovered"
        # It is classified sibling, so it never appears in the unresolved set.
        assert not any(is_in_repo(r.module) for r in refs if r.module == SIBLING_REF)
        assert all(r.module != SIBLING_REF for r in find_unresolved_in_repo_refs())


@pytest.mark.unit
class TestGateWiring:
    """Static proof the guard is a failing-rollup gate, not advisory (Rule 5)."""

    def _ci(self) -> dict[str, Any]:
        data: dict[str, Any] = yaml.safe_load(CI_WORKFLOW.read_text())
        return data

    def test_precommit_hook_present(self) -> None:
        cfg = yaml.safe_load(PRE_COMMIT.read_text())
        ids = {
            h["id"]
            for repo in cfg["repos"]
            if repo.get("repo") == "local"
            for h in repo["hooks"]
        }
        assert "workflow-module-refs" in ids, (
            "pre-commit hook 'workflow-module-refs' missing"
        )

    def test_ci_job_present(self) -> None:
        assert "workflow-module-refs" in self._ci()["jobs"], (
            "ci.yml job 'workflow-module-refs' missing"
        )

    def test_ci_job_runs_the_checker(self) -> None:
        job = self._ci()["jobs"]["workflow-module-refs"]
        run_steps = " ".join(step.get("run", "") for step in job["steps"])
        assert "scripts/ci/check_workflow_module_refs.py" in run_steps

    def test_job_is_in_ci_summary_strict_gates(self) -> None:
        assert "Workflow Module Resolution" in STRICT_GATE_JOBS

    def test_ci_summary_failure_loop_uses_fail_closed_poller(self) -> None:
        run_steps = " ".join(
            step.get("run", "") for step in self._ci()["jobs"]["ci-summary"]["steps"]
        )
        assert "scripts/ci/ci_summary_gate.py" in run_steps
