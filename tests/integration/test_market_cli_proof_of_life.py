"""End-to-end proof for market-owned CLI report output surfaces."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.market_skill_baseline import (
    MARKET_SKILL_SPECS,
    ModelMarketSkillSpec,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    ModelPrLifecycleStartCommand,
)


def _contract_terminal_event(spec: ModelMarketSkillSpec) -> str:
    raw = yaml.safe_load(Path(spec.contract_path).read_text(encoding="utf-8")) or {}
    return str(raw["terminal_event"])


def _write_bin_script(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _pr_lifecycle_input_json() -> str:
    command = ModelPrLifecycleStartCommand(
        correlation_id=uuid4(),
        run_id="market-cli-proof",
        dry_run=True,
        inventory_only=True,
    )
    envelope = ModelEventEnvelope[ModelPrLifecycleStartCommand](
        event_type="omnimarket.pr-lifecycle-orchestrator-start",
        correlation_id=command.correlation_id,
        payload=command,
    )
    return envelope.model_dump_json()


def _command_for_spec(
    spec: ModelMarketSkillSpec, tmp_path: Path
) -> tuple[list[str], dict[str, str]]:
    command = [sys.executable, "-m", spec.module]
    env: dict[str, str] = {}

    if spec.smoke_kind == "aislop_sweep":
        omni_home = tmp_path / "omni_home"
        repo = omni_home / "empty_repo"
        repo.mkdir(parents=True)
        env["OMNI_HOME"] = str(omni_home)
        command.extend(
            [
                "--repos",
                "empty_repo",
                "--dry-run",
                "--severity-threshold",
                "CRITICAL",
            ]
        )
    elif spec.smoke_kind == "pr_lifecycle_orchestrator":
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_bin_script(bin_dir, "gh", "#!/usr/bin/env bash\nexit 0\n")
        env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        env["ONEX_STATE_DIR"] = str(tmp_path / "state")
        command.extend(["--input", _pr_lifecycle_input_json()])
    elif spec.smoke_kind == "pr_polish":
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run_dir = tmp_path / "run"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_bin_script(
            bin_dir,
            "gh",
            """#!/usr/bin/env bash
if [[ "$1" == "pr" && "$2" == "view" ]]; then
  if [[ "$7" == "headRefName" ]]; then
    printf '{"headRefName":"feature/market-proof"}'
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
        )
        _write_bin_script(
            bin_dir,
            "git",
            """#!/usr/bin/env bash
if [[ "$1" == "-C" && "$3" == "rev-parse" ]]; then
  if [[ "$4" == "--abbrev-ref" ]]; then
    printf 'feature/market-proof'
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
        )
        env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        env["ONEX_STATE_DIR"] = str(tmp_path / "state")
        command.extend(
            [
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
        )
    elif spec.smoke_kind == "local_review":
        command.append("--dry-run")
    elif spec.smoke_kind == "coderabbit_triage":
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_bin_script(
            bin_dir,
            "gh",
            """#!/usr/bin/env bash
if [ "$1" = "api" ] && [ "$2" = "graphql" ]; then
  cat <<'JSON'
{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[{"id":"thread-1","isResolved":false,"comments":{"nodes":[{"databaseId":101,"author":{"login":"coderabbitai"},"body":"minor suggestion: rename this variable","path":"src/example.py","url":"https://example.com/thread-1"}]}}]}}}}}
JSON
  exit 0
fi
exit 1
""",
        )
        env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        command.extend(
            [
                "--repo",
                "OmniNode-ai/omnimarket",
                "--pr",
                "1",
                "--dry-run",
            ]
        )
    elif spec.smoke_kind == "session_bootstrap":
        command.extend(["--dry-run", "--state-dir", str(tmp_path / "state")])
    elif spec.smoke_kind == "session_orchestrator":
        fixture_path = tmp_path / "linear-fixture.json"
        fixture_path.write_text(
            json.dumps(
                {
                    "nodes": [
                        {
                            "identifier": "OMN-10404",
                            "title": "Market CLI proof",
                            "priority": 1,
                            "labels": {"nodes": [{"name": "market"}]},
                            "updatedAt": "2026-04-30T00:00:00Z",
                            "children": {"nodes": []},
                        },
                        {
                            "identifier": "OMN-10399",
                            "title": "Ticket pipeline CLI output",
                            "priority": 2,
                            "labels": {"nodes": []},
                            "updatedAt": "2026-04-29T00:00:00Z",
                            "children": {"nodes": []},
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        env["ONEX_SESSION_ORCHESTRATOR_LINEAR_FIXTURE"] = str(fixture_path)
        command.extend(
            [
                "--skip-health",
                "--phase",
                "0",
                "--state-dir",
                str(tmp_path / "state"),
                "--session-id",
                "sess-market-cli-proof",
            ]
        )
    elif spec.smoke_kind == "ticket_pipeline":
        command.extend(["OMN-9530", "--dry-run"])
    else:
        raise AssertionError(f"unhandled smoke kind: {spec.smoke_kind}")

    return command, env


@pytest.mark.parametrize("spec", MARKET_SKILL_SPECS, ids=lambda item: item.skill_name)
@pytest.mark.parametrize("fmt", ["text", "json", "yaml", "markdown"])
def test_each_market_skill_emits_valid_report(
    spec: ModelMarketSkillSpec, fmt: str, tmp_path: Path
) -> None:
    command, env = _command_for_spec(spec, tmp_path)
    command.extend(["--output", fmt])
    merged_env = os.environ.copy()
    merged_env.update(env)

    res = subprocess.run(
        command,
        cwd=Path.cwd(),
        env=merged_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert res.returncode == 0, (
        f"{spec.skill_name} {fmt} failed rc={res.returncode}\n"
        f"stdout={res.stdout}\nstderr={res.stderr}"
    )
    assert res.stdout.strip(), f"{spec.skill_name} {fmt} produced empty stdout"

    if fmt == "json":
        parsed = json.loads(res.stdout)
        assert parsed["skill_name"] == spec.skill_name
        assert parsed["node_name"] == spec.node_name
        assert parsed["terminal_event"] == _contract_terminal_event(spec)
        assert isinstance(parsed["steps"], list)
    elif fmt == "yaml":
        parsed = yaml.safe_load(res.stdout)
        assert parsed["skill_name"] == spec.skill_name
        assert parsed["node_name"] == spec.node_name
        assert parsed["terminal_event"] == _contract_terminal_event(spec)
    elif fmt == "text":
        assert f"OMNIMARKET SKILL: {spec.skill_name}" in res.stdout
        assert "Result" in res.stdout
    elif fmt == "markdown":
        assert "## Execution" in res.stdout
        assert "## Result" in res.stdout
