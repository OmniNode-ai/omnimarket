"""Market-owned skill baseline inventory and smoke runner.

This module intentionally ignores Codex/Claude wrappers. It inventories the
actual omnimarket-owned operator surfaces and exercises their direct entrypoints.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    ModelPrLifecycleStartCommand,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
OMNI_HOME = REPO_ROOT.parent
REPO_ROOT_LABEL = "<omnimarket>"
OMNI_HOME_LABEL = "<omni_home>"
HOME_LABEL = "<home>"
TEMP_MARKET_SKILL_PATTERN = re.compile(
    r"(?:/private)?/var/folders/[^ ]+/T/market-skill-[^/ ]+|/tmp/market-skill-[^/ ]+"
)


class ModelCommandResult(BaseModel):
    """Result of a smoke command or focused pytest invocation."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    command: list[str]
    returncode: int
    summary: dict[str, object] = Field(default_factory=dict)
    stderr: str = ""
    notes: list[str] = Field(default_factory=list)


class ModelContractInventory(BaseModel):
    """Static inventory from a node contract."""

    model_config = ConfigDict(extra="forbid")

    contract_name: str
    node_name: str
    node_type: str
    timeout_ms: int
    terminal_event: str
    inputs: list[str]


class ModelInputDrift(BaseModel):
    """Contract-vs-model field drift inside omnimarket."""

    model_config = ConfigDict(extra="forbid")

    matches: bool
    contract_only_fields: list[str] = Field(default_factory=list)
    model_only_fields: list[str] = Field(default_factory=list)


class ModelMarketSkillSpec(BaseModel):
    """Curated market-owned operator surface."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_name: str
    node_name: str
    module: str
    contract_path: str
    pytest_targets: tuple[str, ...]
    smoke_kind: Literal[
        "aislop_sweep",
        "pr_lifecycle_orchestrator",
        "pr_polish",
        "local_review",
        "coderabbit_triage",
        "session_bootstrap",
        "session_orchestrator",
        "ticket_pipeline",
    ]


class ModelMarketSkillResult(BaseModel):
    """Observed baseline for one market-owned skill surface."""

    model_config = ConfigDict(extra="forbid")

    skill_name: str
    contract: ModelContractInventory
    input_drift: ModelInputDrift
    cli_smoke: ModelCommandResult
    pytest: ModelCommandResult | None = None
    overall_status: Literal["working", "degraded", "failing"]


class ModelMarketSkillBaselineReport(BaseModel):
    """Full market-skill baseline report."""

    model_config = ConfigDict(extra="forbid")

    captured_at: datetime
    repo_root: str
    skills: list[ModelMarketSkillResult]

    @property
    def working_count(self) -> int:
        return sum(1 for item in self.skills if item.overall_status == "working")

    @property
    def degraded_count(self) -> int:
        return sum(1 for item in self.skills if item.overall_status == "degraded")

    @property
    def failing_count(self) -> int:
        return sum(1 for item in self.skills if item.overall_status == "failing")


MARKET_SKILL_SPECS: tuple[ModelMarketSkillSpec, ...] = (
    ModelMarketSkillSpec(
        skill_name="aislop_sweep",
        node_name="node_aislop_sweep",
        module="omnimarket.nodes.node_aislop_sweep",
        contract_path="src/omnimarket/nodes/node_aislop_sweep/contract.yaml",
        pytest_targets=(
            "tests/test_golden_chain_aislop_sweep.py",
            "tests/test_codex_runtime_client.py::test_aislop_sweep_pattern_b_runs_node_end_to_end",
        ),
        smoke_kind="aislop_sweep",
    ),
    ModelMarketSkillSpec(
        skill_name="pr_lifecycle_orchestrator",
        node_name="node_pr_lifecycle_orchestrator",
        module="omnimarket.nodes.node_pr_lifecycle_orchestrator",
        contract_path="src/omnimarket/nodes/node_pr_lifecycle_orchestrator/contract.yaml",
        pytest_targets=(
            "tests/unit/nodes/node_pr_lifecycle_orchestrator/test_main_cli.py",
            "tests/test_golden_chain_pr_lifecycle_orchestrator.py",
            "tests/test_codex_runtime_client.py::test_merge_sweep_pattern_b_runs_pr_lifecycle_end_to_end",
        ),
        smoke_kind="pr_lifecycle_orchestrator",
    ),
    ModelMarketSkillSpec(
        skill_name="pr_polish",
        node_name="node_pr_polish",
        module="omnimarket.nodes.node_pr_polish",
        contract_path="src/omnimarket/nodes/node_pr_polish/contract.yaml",
        pytest_targets=("tests/test_golden_chain_pr_polish.py",),
        smoke_kind="pr_polish",
    ),
    ModelMarketSkillSpec(
        skill_name="local_review",
        node_name="node_local_review",
        module="omnimarket.nodes.node_local_review",
        contract_path="src/omnimarket/nodes/node_local_review/contract.yaml",
        pytest_targets=("tests/test_golden_chain_local_review.py",),
        smoke_kind="local_review",
    ),
    ModelMarketSkillSpec(
        skill_name="coderabbit_triage",
        node_name="node_coderabbit_triage",
        module="omnimarket.nodes.node_coderabbit_triage",
        contract_path="src/omnimarket/nodes/node_coderabbit_triage/contract.yaml",
        pytest_targets=("tests/test_golden_chain_coderabbit_triage.py",),
        smoke_kind="coderabbit_triage",
    ),
    ModelMarketSkillSpec(
        skill_name="session_bootstrap",
        node_name="node_session_bootstrap",
        module="omnimarket.nodes.node_session_bootstrap",
        contract_path="src/omnimarket/nodes/node_session_bootstrap/contract.yaml",
        pytest_targets=(
            "tests/test_golden_chain_session_bootstrap.py",
            "tests/test_codex_runtime_client.py::test_session_bootstrap_pattern_b_runs_node_end_to_end",
        ),
        smoke_kind="session_bootstrap",
    ),
    ModelMarketSkillSpec(
        skill_name="session_orchestrator",
        node_name="node_session_orchestrator",
        module="omnimarket.nodes.node_session_orchestrator",
        contract_path="src/omnimarket/nodes/node_session_orchestrator/contract.yaml",
        pytest_targets=(
            "src/omnimarket/nodes/node_session_orchestrator/tests/test_handler_session_orchestrator.py",
            "tests/unit/test_handler_session_orchestrator_graphql.py",
            "tests/test_codex_runtime_client.py::test_session_orchestrator_pattern_b_runs_node_end_to_end",
        ),
        smoke_kind="session_orchestrator",
    ),
    ModelMarketSkillSpec(
        skill_name="ticket_pipeline",
        node_name="node_ticket_pipeline",
        module="omnimarket.nodes.node_ticket_pipeline",
        contract_path="src/omnimarket/nodes/node_ticket_pipeline/contract.yaml",
        pytest_targets=("tests/test_golden_chain_ticket_pipeline.py",),
        smoke_kind="ticket_pipeline",
    ),
)

_IGNORED_MODEL_FIELDS = {"correlation_id", "requested_at"}


def iter_market_skill_specs() -> tuple[ModelMarketSkillSpec, ...]:
    """Return the curated market-skill surface set."""

    return MARKET_SKILL_SPECS


def _load_contract(spec: ModelMarketSkillSpec) -> ModelContractInventory:
    path = REPO_ROOT / spec.contract_path
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    inputs = list((raw.get("inputs") or {}).keys())
    return ModelContractInventory(
        contract_name=str(raw["name"]),
        node_name=spec.node_name,
        node_type=str(raw["node_type"]),
        timeout_ms=int(raw.get("descriptor", {}).get("timeout_ms", 0)),
        terminal_event=str(raw["terminal_event"]),
        inputs=inputs,
    )


def _resolve_input_model_path(spec: ModelMarketSkillSpec) -> str:
    raw = (
        yaml.safe_load((REPO_ROOT / spec.contract_path).read_text(encoding="utf-8"))
        or {}
    )
    handler = raw.get("handler")
    if isinstance(handler, dict) and handler.get("input_model"):
        return str(handler["input_model"])
    routing = raw.get("handler_routing", {})
    handlers = routing.get("handlers", [])
    if handlers:
        event_model = handlers[0].get("event_model", {})
        module = event_model.get("module")
        name = event_model.get("name")
        if module and name:
            return f"{module}.{name}"
    msg = f"Unable to resolve input model for {spec.skill_name}"
    raise ValueError(msg)


def _load_model_fields(spec: ModelMarketSkillSpec) -> list[str]:
    dotted = _resolve_input_model_path(spec)
    module_name, _, attr_name = dotted.rpartition(".")
    model = getattr(import_module(module_name), attr_name)
    return sorted(
        field for field in model.model_fields if field not in _IGNORED_MODEL_FIELDS
    )


def _compute_input_drift(
    contract: ModelContractInventory, spec: ModelMarketSkillSpec
) -> ModelInputDrift:
    contract_fields = sorted(
        field for field in contract.inputs if field not in _IGNORED_MODEL_FIELDS
    )
    model_fields = _load_model_fields(spec)
    contract_set = set(contract_fields)
    model_set = set(model_fields)
    return ModelInputDrift(
        matches=contract_set == model_set,
        contract_only_fields=sorted(contract_set - model_set),
        model_only_fields=sorted(model_set - contract_set),
    )


@contextmanager
def _fake_gh_script(body: str) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="market-skill-gh-") as tmp:
        path = Path(tmp) / "gh"
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        yield path


@contextmanager
def _fake_bin_scripts(scripts: dict[str, str]) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="market-skill-bin-") as tmp:
        bin_dir = Path(tmp)
        for name, body in scripts.items():
            path = bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        yield bin_dir


def _run_command(
    *,
    command: list[str],
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=merged_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _sanitize_report_value(value: str) -> str:
    """Remove workstation-specific paths from persisted baseline artifacts."""

    sanitized = value.replace(sys.executable, "python")
    sanitized = sanitized.replace(str(REPO_ROOT), REPO_ROOT_LABEL)
    sanitized = sanitized.replace(str(OMNI_HOME), OMNI_HOME_LABEL)
    sanitized = sanitized.replace(str(Path.home()), HOME_LABEL)
    sanitized = TEMP_MARKET_SKILL_PATTERN.sub("<tmp>/market-skill", sanitized)
    return sanitized


def _sanitize_command(command: list[str]) -> list[str]:
    return [_sanitize_report_value(item) for item in command]


def _failure_command_result(
    *,
    stage: str,
    error: BaseException,
    command: list[str] | None = None,
) -> ModelCommandResult:
    return ModelCommandResult(
        passed=False,
        command=_sanitize_command(command or []),
        returncode=1,
        summary={"stage": stage, "error_type": type(error).__name__},
        stderr=str(error),
    )


def _parse_json(stdout: str) -> dict[str, object]:
    parsed = json.loads(stdout)
    if not isinstance(parsed, dict):
        raise ValueError("expected top-level JSON object")
    return parsed


def _summarize_aislop(payload: dict[str, object]) -> dict[str, object]:
    findings = payload.get("findings")
    count = len(findings) if isinstance(findings, list) else 0
    return {
        "status": payload.get("status"),
        "repos_scanned": payload.get("repos_scanned"),
        "findings_count": count,
        "dry_run": payload.get("dry_run"),
    }


def _summarize_pr_lifecycle(payload: dict[str, object]) -> dict[str, object]:
    return {
        "final_state": payload.get("final_state"),
        "prs_inventoried": payload.get("prs_inventoried"),
        "prs_merged": payload.get("prs_merged"),
        "prs_fixed": payload.get("prs_fixed"),
        "prs_verified": payload.get("prs_verified"),
    }


def _summarize_pr_polish(payload: dict[str, object]) -> dict[str, object]:
    return {
        "final_phase": payload.get("final_phase"),
        "pr_number": payload.get("pr_number"),
        "error_message": payload.get("error_message"),
    }


def _summarize_local_review(payload: dict[str, object]) -> dict[str, object]:
    return {
        "current_phase": payload.get("current_phase"),
        "max_iterations": payload.get("max_iterations"),
        "required_clean_runs": payload.get("required_clean_runs"),
        "dry_run": payload.get("dry_run"),
    }


def _summarize_coderabbit(payload: dict[str, object]) -> dict[str, object]:
    return {
        "total_threads": payload.get("total_threads"),
        "blocking_count": payload.get("blocking_count"),
        "suggestion_count": payload.get("suggestion_count"),
        "unknown_count": payload.get("unknown_count"),
        "dry_run": payload.get("dry_run"),
    }


def _summarize_session_bootstrap(payload: dict[str, object]) -> dict[str, object]:
    crons = payload.get("crons_registered")
    return {
        "status": payload.get("status"),
        "crons_registered_count": len(crons) if isinstance(crons, list) else 0,
        "dry_run": payload.get("dry_run"),
    }


def _summarize_session_orchestrator(payload: dict[str, object]) -> dict[str, object]:
    dispatch_queue = payload.get("dispatch_queue", [])
    dispatch_receipts = payload.get("dispatch_receipts", [])
    return {
        "status": payload.get("status"),
        "session_id": "sess-<redacted>" if payload.get("session_id") else None,
        "dry_run": payload.get("dry_run"),
        "dispatch_queue_count": len(dispatch_queue)
        if isinstance(dispatch_queue, list)
        else 0,
        "dispatch_receipt_count": len(dispatch_receipts)
        if isinstance(dispatch_receipts, list)
        else 0,
    }


def _summarize_ticket_pipeline(payload: dict[str, object]) -> dict[str, object]:
    result_summary = payload.get("result_summary")
    if not isinstance(result_summary, dict):
        result_summary = {}
    results = payload.get("steps", payload.get("phase_results", []))
    return {
        "stopped_at": result_summary.get("stopped_at", payload.get("stopped_at")),
        "stop_reason": result_summary.get("stop_reason", payload.get("stop_reason")),
        "ran_phase": result_summary.get("ran_phase", payload.get("ran_phase")),
        "phase_results_count": len(results) if isinstance(results, list) else 0,
        "compiled_dispatch": _ticket_pipeline_compiled_dispatch(results),
    }


def _ticket_pipeline_compiled_dispatch(results: object) -> bool:
    if not isinstance(results, list):
        return False
    for item in results:
        if not isinstance(item, dict):
            continue
        details = item.get("details")
        if (
            item.get("phase", item.get("name")) == "implement"
            and item.get("status") == "succeeded"
            and isinstance(details, dict)
            and details.get("execution_mode") == "compile_only"
        ):
            return True
    return False


def _pr_lifecycle_envelope_json() -> str:
    command = ModelPrLifecycleStartCommand(
        correlation_id=uuid4(),
        run_id="market-skill-baseline",
        dry_run=True,
        inventory_only=True,
    )
    envelope = ModelEventEnvelope[ModelPrLifecycleStartCommand](
        event_type="omnimarket.pr-lifecycle-orchestrator-start",
        correlation_id=command.correlation_id,
        payload=command,
    )
    return envelope.model_dump_json()


def _smoke_aislop_sweep() -> ModelCommandResult:
    command = [
        sys.executable,
        "-m",
        "omnimarket.nodes.node_aislop_sweep",
        "--repos",
        "omnimarket",
        "--dry-run",
        "--severity-threshold",
        "CRITICAL",
    ]
    completed = _run_command(command=command, env={"OMNI_HOME": str(OMNI_HOME)})
    payload = _parse_json(completed.stdout)
    status = str(payload.get("status", ""))
    passed = completed.returncode in (0, 1) and status in {
        "clean",
        "findings",
        "partial",
    }
    notes: list[str] = []
    if completed.returncode == 1 and status == "findings":
        notes.append(
            "findings are expected to exit non-zero; this still proves the node ran"
        )
    return ModelCommandResult(
        passed=passed,
        command=_sanitize_command(command),
        returncode=completed.returncode,
        summary=_summarize_aislop(payload),
        stderr=completed.stderr.strip(),
        notes=notes,
    )


def _smoke_pr_lifecycle_orchestrator() -> ModelCommandResult:
    command = [
        sys.executable,
        "-m",
        "omnimarket.nodes.node_pr_lifecycle_orchestrator",
        "--input",
        _pr_lifecycle_envelope_json(),
    ]
    with (
        tempfile.TemporaryDirectory(prefix="market-skill-pr-lifecycle-") as tmp,
        _fake_gh_script("#!/usr/bin/env bash\nexit 0\n") as gh_path,
    ):
        completed = _run_command(
            command=command,
            env={
                "PATH": f"{gh_path.parent}{os.pathsep}{os.environ.get('PATH', '')}",
                "ONEX_STATE_DIR": str(Path(tmp) / "state"),
            },
        )
    payload = _parse_json(completed.stdout)
    passed = completed.returncode == 0 and payload.get("final_state") == "COMPLETE"
    return ModelCommandResult(
        passed=passed,
        command=_sanitize_command(command),
        returncode=completed.returncode,
        summary=_summarize_pr_lifecycle(payload),
        stderr=completed.stderr.strip(),
    )


def _smoke_pr_polish() -> ModelCommandResult:
    with tempfile.TemporaryDirectory(prefix="market-skill-pr-polish-") as tmp:
        tmp_path = Path(tmp)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run_dir = tmp_path / "run"
        command = [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_pr_polish",
            "--repo",
            "OmniNode-ai/omnimarket",
            "--pr-number",
            "1",
            "--dry-run",
            "--no-push",
            "--no-automerge",
            "--worktree-path",
            str(worktree),
            "--run-dir",
            str(run_dir),
        ]
        scripts = {
            "gh": """#!/usr/bin/env bash
if [[ "$1" == "pr" && "$2" == "view" ]]; then
  if [[ "$7" == "headRefName" ]]; then
    printf '{"headRefName":"feature/market-baseline"}'
    exit 0
  fi
  printf '{}'
  exit 0
fi
if [[ "$1" == "api" && "$2" == "graphql" ]]; then
  printf '{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[]}}}}}'
  exit 0
fi
echo "unexpected gh invocation: $*" >&2
exit 1
""",
            "git": """#!/usr/bin/env bash
if [[ "$1" == "-C" && "$3" == "rev-parse" ]]; then
  if [[ "$4" == "--abbrev-ref" ]]; then
    printf 'feature/market-baseline'
    exit 0
  fi
  printf 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef'
  exit 0
fi
if [[ "$1" == "-C" && "$3" == "diff" ]]; then
  exit 0
fi
echo "unexpected git invocation: $*" >&2
exit 1
""",
        }
        with _fake_bin_scripts(scripts) as bin_dir:
            completed = _run_command(
                command=command,
                env={
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "ONEX_STATE_DIR": str(tmp_path / "state"),
                },
            )
    payload = _parse_json(completed.stdout)
    passed = completed.returncode == 0 and payload.get("final_phase") == "done"
    return ModelCommandResult(
        passed=passed,
        command=_sanitize_command(command),
        returncode=completed.returncode,
        summary=_summarize_pr_polish(payload),
        stderr=completed.stderr.strip(),
    )


def _smoke_local_review() -> ModelCommandResult:
    command = [sys.executable, "-m", "omnimarket.nodes.node_local_review", "--dry-run"]
    completed = _run_command(command=command)
    payload = _parse_json(completed.stdout)
    passed = completed.returncode == 0 and payload.get("current_phase") == "init"
    return ModelCommandResult(
        passed=passed,
        command=_sanitize_command(command),
        returncode=completed.returncode,
        summary=_summarize_local_review(payload),
        stderr=completed.stderr.strip(),
    )


def _smoke_coderabbit_triage() -> ModelCommandResult:
    command = [
        sys.executable,
        "-m",
        "omnimarket.nodes.node_coderabbit_triage",
        "--repo",
        "OmniNode-ai/omnimarket",
        "--pr",
        "1",
        "--dry-run",
    ]
    gh_body = """#!/usr/bin/env bash
if [ "$1" = "api" ] && [ "$2" = "graphql" ]; then
  cat <<'JSON'
{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[{"id":"thread-1","isResolved":false,"comments":{"nodes":[{"databaseId":101,"author":{"login":"coderabbitai"},"body":"minor suggestion: rename this variable","path":"src/example.py","url":"https://example.com/thread-1"}]}}]}}}}}
JSON
  exit 0
fi
exit 1
"""
    with _fake_gh_script(gh_body) as gh_path:
        completed = _run_command(
            command=command,
            env={"PATH": f"{gh_path.parent}{os.pathsep}{os.environ.get('PATH', '')}"},
        )
    payload = _parse_json(completed.stdout)
    passed = (
        completed.returncode == 0
        and payload.get("total_threads") == 1
        and payload.get("suggestion_count") == 1
    )
    return ModelCommandResult(
        passed=passed,
        command=_sanitize_command(command),
        returncode=completed.returncode,
        summary=_summarize_coderabbit(payload),
        stderr=completed.stderr.strip(),
    )


def _smoke_session_bootstrap() -> ModelCommandResult:
    with tempfile.TemporaryDirectory(prefix="market-skill-session-bootstrap-") as tmp:
        command = [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_session_bootstrap",
            "--dry-run",
            "--state-dir",
            str(Path(tmp) / "state"),
        ]
        completed = _run_command(command=command)
    payload = _parse_json(completed.stdout)
    passed = completed.returncode == 0 and payload.get("status") == "ready"
    return ModelCommandResult(
        passed=passed,
        command=_sanitize_command(command),
        returncode=completed.returncode,
        summary=_summarize_session_bootstrap(payload),
        stderr=completed.stderr.strip(),
    )


def _smoke_session_orchestrator() -> ModelCommandResult:
    with tempfile.TemporaryDirectory(
        prefix="market-skill-session-orchestrator-"
    ) as tmp:
        tmp_path = Path(tmp)
        state_dir = tmp_path / "state"
        fixture_path = tmp_path / "linear-fixture.json"
        fixture_path.write_text(
            json.dumps(
                {
                    "nodes": [
                        {
                            "identifier": "OMN-10400",
                            "title": "Real surfaces CLI output",
                            "priority": 1,
                            "labels": {"nodes": [{"name": "market"}]},
                            "updatedAt": "2026-04-20T00:00:00Z",
                            "children": {"nodes": []},
                        },
                        {
                            "identifier": "OMN-10399",
                            "title": "Ticket pipeline CLI output",
                            "priority": 2,
                            "labels": {"nodes": []},
                            "updatedAt": "2026-04-18T00:00:00Z",
                            "children": {"nodes": []},
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        command = [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_session_orchestrator",
            "--skip-health",
            "--phase",
            "0",
            "--state-dir",
            str(state_dir),
            "--session-id",
            "sess-market-baseline",
            "--output-json",
        ]
        completed = _run_command(
            command=command,
            env={
                "ONEX_SESSION_ORCHESTRATOR_LINEAR_FIXTURE": str(fixture_path),
            },
        )
        payload = _parse_json(completed.stdout)
        dispatch_receipts_raw = payload.get("dispatch_receipts")
        dispatch_receipts = (
            dispatch_receipts_raw if isinstance(dispatch_receipts_raw, list) else []
        )
        receipt_payloads = [
            json.loads(item) for item in dispatch_receipts if isinstance(item, str)
        ]
        artifact_paths = [
            Path(str(item.get("dispatch_artifact_path")))
            for item in receipt_payloads
            if item.get("dispatch_artifact_path")
        ]
        evidence_paths = [
            state_dir / "in_flight.yaml",
            state_dir / "ledger.jsonl",
            *artifact_paths,
            *state_dir.glob("rsd-scored-*.yaml"),
        ]
        dispatch_queue = payload.get("dispatch_queue")
        passed = (
            completed.returncode == 0
            and payload.get("status") == "complete"
            and isinstance(dispatch_queue, list)
            and len(dispatch_queue) == 2
            and len(receipt_payloads) == 2
            and all(path.exists() for path in evidence_paths)
        )
        notes = [
            "smoke bypasses health probes and uses a deterministic Linear fixture "
            "to exercise Phase 2 scoring plus Phase 3 dispatch artifacts"
        ]
        return ModelCommandResult(
            passed=passed,
            command=_sanitize_command(command),
            returncode=completed.returncode,
            summary=_summarize_session_orchestrator(payload),
            stderr="skip_health=True" if completed.stderr.strip() else "",
            notes=notes,
        )


def _smoke_ticket_pipeline() -> ModelCommandResult:
    command = [
        sys.executable,
        "-m",
        "omnimarket.nodes.node_ticket_pipeline",
        "OMN-9360",
        "--dry-run",
    ]
    completed = _run_command(command=command)
    payload = _parse_json(completed.stdout)
    result_summary = payload.get("result_summary")
    if not isinstance(result_summary, dict):
        result_summary = {}
    passed = (
        completed.returncode == 0
        and result_summary.get("stopped_at", payload.get("stopped_at")) == "blocked"
        and result_summary.get("stop_reason", payload.get("stop_reason"))
        == "not_implemented"
        and _ticket_pipeline_compiled_dispatch(
            payload.get("steps", payload.get("phase_results"))
        )
    )
    return ModelCommandResult(
        passed=passed,
        command=_sanitize_command(command),
        returncode=completed.returncode,
        summary=_summarize_ticket_pipeline(payload),
        stderr="task_directory_missing" if completed.stderr.strip() else "",
        notes=[
            "bounded slice wires PRE_FLIGHT plus compile-only IMPLEMENT; LOCAL_REVIEW should block as not_implemented; stopped_at=blocked is a stop state"
        ],
    )


SMOKE_RUNNERS: dict[str, Callable[[], ModelCommandResult]] = {
    "aislop_sweep": _smoke_aislop_sweep,
    "pr_lifecycle_orchestrator": _smoke_pr_lifecycle_orchestrator,
    "pr_polish": _smoke_pr_polish,
    "local_review": _smoke_local_review,
    "coderabbit_triage": _smoke_coderabbit_triage,
    "session_bootstrap": _smoke_session_bootstrap,
    "session_orchestrator": _smoke_session_orchestrator,
    "ticket_pipeline": _smoke_ticket_pipeline,
}


def run_cli_smoke(spec: ModelMarketSkillSpec) -> ModelCommandResult:
    """Run the curated direct-entrypoint smoke for one market skill."""

    return SMOKE_RUNNERS[spec.smoke_kind]()


def run_pytest_targets(spec: ModelMarketSkillSpec) -> ModelCommandResult:
    """Run focused proof tests for one market skill."""

    command = [sys.executable, "-m", "pytest", *spec.pytest_targets, "-q"]
    completed = _run_command(command=command, env={"ONEX_STATE_DIR": ""})
    passed = completed.returncode == 0
    summary: dict[str, object] = {"targets": list(spec.pytest_targets)}
    if passed:
        summary["result"] = "passed"
    return ModelCommandResult(
        passed=passed,
        command=_sanitize_command(command),
        returncode=completed.returncode,
        summary=summary,
        stderr=completed.stderr.strip(),
        notes=[completed.stdout.strip()] if completed.stdout.strip() else [],
    )


def _overall_status(
    *,
    input_drift: ModelInputDrift,
    cli_smoke: ModelCommandResult,
    pytest_result: ModelCommandResult | None,
) -> Literal["working", "degraded", "failing"]:
    if (
        input_drift.matches
        and cli_smoke.passed
        and (pytest_result is None or pytest_result.passed)
    ):
        return "working"
    if cli_smoke.passed or (pytest_result is not None and pytest_result.passed):
        return "degraded"
    if not input_drift.matches:
        return "failing"
    return "failing"


def _fallback_contract(spec: ModelMarketSkillSpec) -> ModelContractInventory:
    return ModelContractInventory(
        contract_name=spec.node_name,
        node_name=spec.node_name,
        node_type="unknown",
        timeout_ms=0,
        terminal_event="unknown",
        inputs=[],
    )


def capture_market_skill_baseline(
    *,
    run_pytest: bool = True,
    skill_names: set[str] | None = None,
) -> ModelMarketSkillBaselineReport:
    """Capture the current market-only baseline."""

    results: list[ModelMarketSkillResult] = []
    for spec in iter_market_skill_specs():
        if skill_names and spec.skill_name not in skill_names:
            continue
        try:
            contract = _load_contract(spec)
        except Exception as exc:
            contract = _fallback_contract(spec)
            input_drift = ModelInputDrift(
                matches=False,
                contract_only_fields=[],
                model_only_fields=[],
            )
            cli_smoke = _failure_command_result(stage="load_contract", error=exc)
            results.append(
                ModelMarketSkillResult(
                    skill_name=spec.skill_name,
                    contract=contract,
                    input_drift=input_drift,
                    cli_smoke=cli_smoke,
                    pytest=None,
                    overall_status="failing",
                )
            )
            continue

        try:
            input_drift = _compute_input_drift(contract, spec)
        except Exception as exc:
            input_drift = ModelInputDrift(
                matches=False,
                contract_only_fields=[],
                model_only_fields=[],
            )
            cli_smoke = _failure_command_result(stage="compute_input_drift", error=exc)
            results.append(
                ModelMarketSkillResult(
                    skill_name=spec.skill_name,
                    contract=contract,
                    input_drift=input_drift,
                    cli_smoke=cli_smoke,
                    pytest=None,
                    overall_status="failing",
                )
            )
            continue

        try:
            cli_smoke = run_cli_smoke(spec)
        except Exception as exc:
            cli_smoke = _failure_command_result(stage="cli_smoke", error=exc)

        pytest_result = None
        if run_pytest:
            try:
                pytest_result = run_pytest_targets(spec)
            except Exception as exc:
                pytest_result = _failure_command_result(stage="pytest", error=exc)
        results.append(
            ModelMarketSkillResult(
                skill_name=spec.skill_name,
                contract=contract,
                input_drift=input_drift,
                cli_smoke=cli_smoke,
                pytest=pytest_result,
                overall_status=_overall_status(
                    input_drift=input_drift,
                    cli_smoke=cli_smoke,
                    pytest_result=pytest_result,
                ),
            )
        )
    return ModelMarketSkillBaselineReport(
        captured_at=datetime.now(tz=UTC),
        repo_root=REPO_ROOT_LABEL,
        skills=results,
    )


def render_markdown(report: ModelMarketSkillBaselineReport) -> str:
    """Render a compact markdown baseline report."""

    cohort_date = report.captured_at.date().isoformat()
    lines = [
        "# Market Skill Baseline",
        "",
        f"Captured at: `{report.captured_at.isoformat()}`",
        f"Baseline window: `{cohort_date}` capture cohort; captured_at is the exact regeneration time.",
        f"Repo root: `{report.repo_root}`",
        "",
        "## Summary",
        "",
        f"- Working: `{report.working_count}`",
        f"- Degraded: `{report.degraded_count}`",
        f"- Failing: `{report.failing_count}`",
        "",
        "## Inventory",
        "",
        "| Skill | Node | Contract | CLI smoke | Focused tests | Status |",
        "|-------|------|----------|-----------|---------------|--------|",
    ]
    for item in report.skills:
        pytest_state = "not-run"
        if item.pytest is not None:
            pytest_state = "pass" if item.pytest.passed else "fail"
        cli_state = "pass" if item.cli_smoke.passed else "fail"
        lines.append(
            "| "
            f"{item.skill_name} | "
            f"{item.contract.node_name} | "
            f"{item.contract.contract_name} | "
            f"{cli_state} | "
            f"{pytest_state} | "
            f"{item.overall_status} |"
        )
    lines.extend(["", "## Details", ""])
    for item in report.skills:
        lines.extend(
            [
                f"### {item.skill_name}",
                "",
                f"- Node: `{item.contract.node_name}`",
                f"- Contract: `{item.contract.contract_name}`",
                f"- Node type: `{item.contract.node_type}`",
                f"- Timeout: `{item.contract.timeout_ms}`",
                f"- Terminal event: `{item.contract.terminal_event}`",
                f"- Inputs: `{', '.join(item.contract.inputs)}`",
                f"- Contract/model input match: `{item.input_drift.matches}`",
                f"- CLI smoke status: `{'pass' if item.cli_smoke.passed else 'fail'}`",
                f"- CLI smoke summary: `{json.dumps(item.cli_smoke.summary, sort_keys=True)}`",
            ]
        )
        if item.input_drift.contract_only_fields:
            lines.append(
                f"- Contract-only inputs: `{', '.join(item.input_drift.contract_only_fields)}`"
            )
        if item.input_drift.model_only_fields:
            lines.append(
                f"- Model-only inputs: `{', '.join(item.input_drift.model_only_fields)}`"
            )
        if item.cli_smoke.notes:
            lines.append(f"- CLI smoke notes: `{' | '.join(item.cli_smoke.notes)}`")
        if item.cli_smoke.stderr:
            lines.append(f"- CLI smoke stderr: `{item.cli_smoke.stderr}`")
        if item.pytest is not None:
            lines.append(
                f"- Focused tests: `{'pass' if item.pytest.passed else 'fail'}`"
            )
            targets = cast(list[str], item.pytest.summary["targets"])
            lines.append(f"- Focused test targets: `{', '.join(targets)}`")
            if item.pytest.notes:
                lines.append(
                    f"- Focused test output: `{' | '.join(item.pytest.notes)}`"
                )
            if item.pytest.stderr:
                lines.append(f"- Focused test stderr: `{item.pytest.stderr}`")
        lines.append("")
    return "\n".join(lines).rstrip()
