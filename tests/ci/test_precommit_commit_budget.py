# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22364: the commit stage stays inside its time budget, and no check is lost.

A commit that touched five files evaluated 186 hook entries here, ran 80 of
them and took 986 s (16 minutes) on an idle lab host, longer on a loaded one. One-runtime lanes ended
at their runtime limit with nothing pushed. The cost was whole-repo scans and
suites, each its own ``uv run`` process, that already run as required CI jobs.

Those hooks now carry ``stages: [manual]``. A manual hook never runs at commit
and is never auto-installed (``pre-commit run --hook-stage manual --all-files``
runs them by hand). This file is the registry that keeps the move honest. A
manual hook must name the CI job that enforces it, and that job must

* exist in the workflow it is registered under,
* be gated by CI Summary (``scripts/ci/ci_summary_gate.py``),
* have no condition that can skip it other than ``always()``, and not be
  ``continue-on-error``,
* run the hook's command (every needle appears in the job's ``run`` steps).

Falsifier: set ``stages: [manual]`` on any hook that is not registered, drop or
rename a registered job, remove its command from the job, or remove the job
from CI Summary's gate list, and a named test here fails.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci import ci_summary_gate

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PRECOMMIT = REPO_ROOT / ".pre-commit-config.yaml"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# Manual hooks that predate this registry. They are not CI-backed commit-stage
# moves; each has its own reason in .pre-commit-config.yaml.
PREEXISTING_MANUAL: frozenset[str] = frozenset()

GATED_CONTEXTS: frozenset[str] = frozenset(
    (
        *ci_summary_gate.STRICT_GATE_JOBS,
        *ci_summary_gate.SKIPPABLE_GATE_JOBS,
        *ci_summary_gate.EXPECTED_EXTERNAL_CONTEXTS,
    )
)

# Commit-stage hooks that take no filenames and always run. The ceiling is
# today's count; lower it when one is scoped or moved, never raise it.
UNSCOPED_COMMIT_STAGE_CEILING = 3


@dataclass(frozen=True)
class Home:
    """The CI job that enforces a hook that left the commit stage."""

    workflow: str
    job: str
    needles: tuple[str, ...]


MOVED: Mapping[str, Home] = {
    "node-auto-wiring-in-process": Home(
        "ci.yml",
        "lint",
        ("tests/unit/contracts/test_every_node_auto_wires_in_process_omn17427.py",),
    ),
    "routing-sources-omn20601": Home(
        "ci.yml",
        "lint",
        (
            "tests/test_routing_sources_omn20601.py tests/unit/nodes/node_deployment_fact_gate_effect/test_routing_in_code_omn20601.py tests/unit/delegation/test_roi_overlay_routing_omn14001.py",
        ),
    ),
    "handler-dispatch-entrypoint": Home(
        "ci.yml",
        "lint",
        ("omnimarket.validators.handler_dispatch_entrypoint src/omnimarket",),
    ),
    "harness-invocation-check": Home(
        "ci.yml", "lint", ("omnimarket.nodes.node_harness_invocation_check_effect",)
    ),
    "walkable-state-machines": Home(
        "ci.yml",
        "lint",
        (
            "tests/unit/contracts/test_walkable_state_machines.py tests/unit/contracts/test_no_untyped_fsm_dialect.py",
        ),
    ),
    "retired-model-references": Home(
        "ci.yml",
        "lint",
        ("-m omnimarket.nodes.node_retired_model_reference_check_effect",),
    ),
    "deployment-fact-gate": Home(
        "deployment-fact-gate.yml",
        "deployment-fact-gate",
        ("omnimarket.nodes.node_deployment_fact_gate_effect",),
    ),
    "morning-phase-tools-guard": Home(
        "ci.yml",
        "lint",
        (
            "pytest tests/test_morning_phase_tools_wiring.py tests/nodes/node_morning_ground_state_orchestrator/test_morning_phase_tools_guard.py",
        ),
    ),
    "no-coding-plan-endpoint": Home(
        "no-coding-plan-endpoint.yml",
        "no-coding-plan-endpoint",
        ("omnimarket.validators.no_coding_plan_endpoint",),
    ),
    "omni-home-no-silent-default": Home(
        "ci.yml", "lint", ("tests/test_omni_home_no_silent_default_omn16856.py",)
    ),
    "local-ingress-alias-collision": Home(
        "contract-topic-closure.yml",
        "local-ingress-alias-collision",
        ("omnimarket.validators.local_ingress_alias_collision",),
    ),
    "rls-read-tenant-seam": Home(
        "ci.yml", "lint", ("scripts/ci/check_rls_read_tenant_seam.py",)
    ),
    "registry-projection-reconcile": Home(
        "ci.yml", "lint", ("scripts/ci/check_registry_projection_reconcile.py",)
    ),
    "arch-unimported-handler-check": Home(
        "ci.yml", "lint", ("scripts/ci/check_unimported_handlers.py",)
    ),
    "projection-cursor-declared": Home(
        "ci.yml",
        "lint",
        (
            "omnimarket.nodes.node_contract_projection_check_effect.runtime_projection_contract_check --rule cursor",
        ),
    ),
    "routing-input-model-fit": Home(
        "contract-topic-closure.yml",
        "routing-input-model-fit",
        ("omnimarket.validators.routing_input_model_fit src/omnimarket",),
    ),
    "contract-topic-graph": Home(
        "ci.yml",
        "contract-topic-graph",
        ("omnimarket.validators.contract_topic_graph --scope omnimarket",),
    ),
    "house-inference-credential-gate": Home(
        "ci.yml", "lint", ("scripts/ci/check_backend_secret_discipline.py",)
    ),
    "key-grain-declared": Home(
        "contract-topic-closure.yml",
        "key-grain-declared",
        ("omnimarket.validators.key_grain_declared",),
    ),
    "state-coverage-gate": Home(
        "state-coverage-gate.yml",
        "state-coverage-gate",
        (
            "scripts/validate_state_coverage.py --check-changed",
            "--strict --check-stale",
        ),
    ),
    "arch-node-metadata-dependency-scope": Home(
        "ci.yml", "lint", ("scripts/ci/check_node_metadata_dependencies.py",)
    ),
    "internal-relation-tenant-posture": Home(
        "ci.yml",
        "lint",
        ("scripts/validation/check_internal_relation_tenant_posture.py",),
    ),
    "projection-contract-access": Home(
        "ci.yml",
        "lint",
        (
            "omnimarket.nodes.node_contract_projection_check_effect.runtime_projection_contract_check --rule access",
        ),
    ),
    "contract-publish-parity-gate": Home(
        "ci.yml", "lint", ("scripts/ci/check_contract_publish_parity.py",)
    ),
    "skill-mapping-input-coverage-gate": Home(
        "skill-mapping-input-coverage-gate.yml",
        "skill-mapping-input-coverage-gate",
        ("scripts/validate_skill_mapping_input_coverage.py",),
    ),
    "contract-sweep-gate": Home(
        "ci.yml",
        "contract-sweep-gate",
        ("scripts/ci/run_contract_sweep_gate.py --strict",),
    ),
    "node-orphan-graph": Home(
        "node-drift-gate.yml",
        "node-drift-gate",
        ("scripts/validate_node_drift.py --check-all --check-orphan-nodes --strict",),
    ),
    "subscriber-dispatcher-resolution": Home(
        "subscriber-dispatcher-resolution.yml",
        "subscriber-dispatcher-resolution",
        ("scripts/ci/check_subscriber_dispatcher_resolution.sh",),
    ),
    "mixed-category-routing": Home(
        "contract-topic-closure.yml",
        "mixed-category-routing",
        ("scripts/ci/check_mixed_category_routing.sh",),
    ),
    "validate-no-transport-imports": Home(
        "ci.yml",
        "lint",
        ("scripts/ci/check_unimported_handlers.py --transport-imports",),
    ),
    "no-noncanonical-lifecycle-classes": Home(
        "ci.yml",
        "no-noncanonical-lifecycle-classes",
        (
            "omnibase_core.validators.no_noncanonical_lifecycle_classes",
            "--allowlist .onex_ratchets/noncanonical_class_allowlist.yaml src/omnimarket",
        ),
    ),
    "check-projection-exposure-drift": Home(
        "projection-exposure-drift-gate.yml",
        "projection-exposure-drift-gate",
        (
            "omnibase_core.validators.projection_exposure_drift src --fail-on-omission --allowlist .projection-exposure-allowlist.yaml",
        ),
    ),
    "check-fsm-handler-drift": Home(
        "validator-fsm-handler-drift.yml",
        "fsm-handler-drift",
        ("omnibase_core.validators.fsm_handler_drift src/",),
    ),
    "dep-health-gate": Home(
        "ci.yml",
        "dep-health",
        (
            "scripts/ci/run_dep_health_sweep.py",
            "--severity-threshold CRITICAL",
            "--delta-mode",
        ),
    ),
    "canonical-file-shape": Home(
        "ci.yml", "canonical-file-shape", ("--rules file-shape",)
    ),
    "check-url-authority": Home(
        "url-authority-gate.yml",
        "url-authority-gate",
        (
            "omnibase_core.validation.validator_url_authority",
            "--all --repo omnimarket --repo-root .",
        ),
    ),
    "check-canonical-inference": Home(
        "canonical-inference-gate.yml",
        "canonical-inference-gate",
        (
            "omnibase_core.validation.validator_canonical_inference",
            "--all --repo omnimarket --repo-root .",
        ),
    ),
    "validate-validator-requirements": Home(
        "validate-validator-requirements.yml",
        "validate-validator-requirements",
        (
            "omnibase_core.validation.validator_requirements_consumer",
            "--repo omnimarket",
        ),
    ),
}

# Hooks whose CI twin is report-only by design: they print findings and exit 0
# whatever they find, so they never blocked a commit and no enforcement moves.
REPORT_ONLY: Mapping[str, Home] = {
    "report-contract-walk": Home(
        "contract-walker.yml",
        "contract-walker",
        ("omnibase_core.validation.validator_contract_walker",),
    ),
}


def _hook_entries() -> list[dict[str, Any]]:
    """Every hook entry in the config; an id may be declared more than once."""
    config = yaml.safe_load(PRECOMMIT.read_text(encoding="utf-8"))
    return [hook for repo in config["repos"] for hook in repo.get("hooks", [])]


def _job(workflow: str, job_id: str) -> dict[str, Any]:
    document = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    jobs: dict[str, Any] = document["jobs"]
    assert job_id in jobs, f"{workflow} has no job {job_id}"
    job: dict[str, Any] = jobs[job_id]
    return job


def _run_text(job: Mapping[str, Any]) -> str:
    runs = [str(step["run"]) for step in job["steps"] if step.get("run")]
    flat = re.sub(r"\\\s*\n\s*", " ", "\n".join(runs))
    return " ".join(flat.split())


def test_every_manual_hook_is_registered_with_a_ci_home() -> None:
    manual = {
        hook["id"] for hook in _hook_entries() if "manual" in (hook.get("stages") or [])
    }
    registered = set(MOVED) | set(REPORT_ONLY) | PREEXISTING_MANUAL
    assert manual == registered, (
        "a hook left the commit stage without a registered CI home, "
        f"or a registered hook is back at commit: {sorted(manual ^ registered)}"
    )


@pytest.mark.parametrize("hook_id", sorted(set(MOVED) | set(REPORT_ONLY)))
def test_moved_hook_is_manual_only(hook_id: str) -> None:
    entries = [hook for hook in _hook_entries() if hook["id"] == hook_id]
    assert entries, f"{hook_id} is not declared"
    assert all(hook.get("stages") == ["manual"] for hook in entries), (
        f"every declaration of {hook_id} must be manual-only"
    )


@pytest.mark.parametrize("hook_id", sorted(MOVED))
def test_moved_hook_has_a_gated_ci_job_that_runs_its_command(hook_id: str) -> None:
    home = MOVED[hook_id]
    job = _job(home.workflow, home.job)
    name = job.get("name", home.job)
    assert name in GATED_CONTEXTS, (
        f"{home.workflow}:{home.job} ({name!r}) is not a CI Summary gate, so "
        f"moving {hook_id} out of the commit stage would leave it unenforced"
    )
    assert job.get("if") in (None, "always()"), (
        f"{home.workflow}:{home.job} can be skipped by {job.get('if')!r}"
    )
    assert job.get("continue-on-error") is not True
    text = _run_text(job)
    for needle in home.needles:
        assert needle in text, (
            f"{home.workflow}:{home.job} no longer runs {needle!r} for {hook_id}"
        )


@pytest.mark.parametrize("hook_id", sorted(REPORT_ONLY))
def test_report_only_hook_has_its_report_only_ci_twin(hook_id: str) -> None:
    home = REPORT_ONLY[hook_id]
    job = _job(home.workflow, home.job)
    assert "report-only" in str(job.get("name")), (
        f"{home.workflow}:{home.job} is no longer report-only, so {hook_id} must be "
        "registered in MOVED with a CI Summary gated job"
    )
    text = _run_text(job)
    for needle in home.needles:
        assert needle in text


def test_commit_stage_keeps_a_ceiling_on_unscoped_whole_repo_hooks() -> None:
    """A new whole-repo hook that fires on every commit needs a CI home first."""
    unscoped = sorted(
        hook["id"]
        for hook in _hook_entries()
        if (hook.get("stages") or ["pre-commit"]) == ["pre-commit"]
        and hook.get("pass_filenames") is False
        and hook.get("always_run")
    )
    assert len(unscoped) <= UNSCOPED_COMMIT_STAGE_CEILING, unscoped
