# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane census asks, per compose project, where the compose file lives and which version runs (OMN-19998).

A survey found compose projects run from a deleted worktree, a scratch folder or a second clone, and a stack mixed from two
release trees; the census only listed containers. The census run now reads the docker inventory once, calls the compose project
census node with the policy overlay, prints one ``compose_project_check`` line and exits 30 on a finding. The hosts here are fixtures.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_lane_census import (
    ModelLaneCensusRunResult,
    ModelLaneCensusSchedule,
)
from omnimarket.nodes.node_compose_project_census_compute.models.model_compose_project_census import (
    ModelComposeCensusHost,
    ModelComposeCensusImage,
    ModelComposeCensusPolicy,
    ModelComposeCensusProject,
)
from omnimarket.nodes.node_lane_census_effect import __main__ as node_main
from omnimarket.nodes.node_lane_census_effect.handlers import (
    adapter_lane_census_schedule as schedule_module,
)
from omnimarket.nodes.node_lane_census_effect.handlers.adapter_compose_project_check import (
    FORMAT,
    ComposeProjectCheckStep,
    ModelComposeProjectCheckCommand,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(schedule_module.__file__).resolve().parents[1]
LABELS = (
    "com.docker.compose.project",
    "com.docker.compose.service",
    "com.docker.compose.project.working_dir",
    "com.docker.compose.project.config_files",
)


def policy() -> ModelComposeCensusPolicy:
    return ModelComposeCensusPolicy(
        worktree_segments=("worktrees", "scratch"),
        hosts=(
            ModelComposeCensusHost(
                name="host-a", aliases=("node-a",), clone_roots=("/srv/clones",)
            ),
        ),
        projects=(
            ModelComposeCensusProject(
                project="web",
                hosts=("host-a",),
                compose_file="web/docker-compose.yml",
                images=(ModelComposeCensusImage(service="web", image="web:1.0"),),
            ),
        ),
    )


def command(
    *, hostname: str = "Node-A.example", pol: ModelComposeCensusPolicy | None = None
) -> ModelComposeProjectCheckCommand:
    return ModelComposeProjectCheckCommand(
        policy=policy() if pol is None else pol,
        hostname=hostname,
        home_dir="/srv/u",
        docker_binary="docker",
        timeout_s=30,
    )


def line(
    name: str = "web-1",
    image: str = "web:1.0",
    project: str = "web",
    working_dir: str = "/srv/clones/web",
    config_files: str = "/srv/clones/web/docker-compose.yml",
) -> str:
    return "\t".join(
        [name, "running", "Up 1 hour", image, project, "web", working_dir, config_files]
    )


def docker(
    stdout: str = "", code: int = 0, stderr: str = ""
) -> tuple[list[list[str]], object]:
    calls: list[list[str]] = []

    def run(argv: Sequence[str], timeout_s: int) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), code, stdout, stderr)

    return calls, run


def handler(stdout: str = "", code: int = 0, stderr: str = "", dirs: bool = True):
    calls, run = docker(stdout, code, stderr)
    return calls, ComposeProjectCheckStep(run=run, is_dir=lambda _p: dirs)


def test_golden_clean_inventory_is_read_once_with_a_field_format() -> None:
    calls, step = handler(line() + "\n")
    result = step.handle(command())
    assert result.outcome == "clean"
    assert result.host == "host-a"
    assert result.findings == ()
    assert len(calls) == 1
    assert calls[0][:5] == ["docker", "ps", "-a", "--no-trunc", "--format"]
    assert "json" not in FORMAT
    assert all(label in FORMAT for label in LABELS)


def test_a_compose_directory_under_a_worktree_is_drift() -> None:
    bad = line(
        working_dir="/srv/clones/worktrees/web",
        config_files="/srv/clones/worktrees/web/docker-compose.yml",
    )
    _, step = handler(bad + "\n")
    result = step.handle(command())
    assert result.outcome == "drift"
    assert "compose_dir_outside_clone" in {f.kind.value for f in result.findings}


def test_an_absent_compose_directory_is_named() -> None:
    _, step = handler(line() + "\n", dirs=False)
    result = step.handle(command())
    assert "compose_file_missing" in {f.kind.value for f in result.findings}


def test_a_running_image_that_is_not_the_declared_one_is_drift() -> None:
    _, step = handler(line(image="web:2.0") + "\n")
    result = step.handle(command())
    assert [f.kind.value for f in result.findings] == ["version_mismatch"]


@pytest.mark.parametrize(
    ("code", "stderr"),
    [(1, "daemon down"), (127, "no such file"), (124, "docker exceeded 30s")],
)
def test_error_chain_a_docker_failure_is_unobservable_never_clean(
    code: int, stderr: str
) -> None:
    _, step = handler("", code, stderr)
    result = step.handle(command())
    assert result.outcome == "inventory-unobservable"
    assert result.findings == ()
    assert any(stderr in d for d in result.diagnostics)


def test_error_chain_a_short_inventory_line_is_unobservable() -> None:
    _, step = handler("web-1\trunning\n")
    result = step.handle(command())
    assert result.outcome == "inventory-unobservable"
    assert result.findings == ()


def test_error_chain_a_hostname_that_matches_no_policy_host() -> None:
    calls, step = handler(line() + "\n")
    result = step.handle(command(hostname="other-host"))
    assert result.outcome == "host-unmatched"
    assert calls == []


def test_error_chain_no_policy_is_skipped_and_reads_nothing() -> None:
    calls, step = handler(line() + "\n")
    result = step.handle(
        ModelComposeProjectCheckCommand(
            policy=None,
            hostname="node-a",
            home_dir="/srv/u",
            docker_binary="docker",
            timeout_s=30,
        )
    )
    assert result.outcome == "skipped-no-policy"
    assert calls == []


# --- the census run's exit code ---------------------------------------------------------------------------------------


def census_result(exit_code: int, observed: bool = True) -> ModelLaneCensusRunResult:
    return ModelLaneCensusRunResult(
        exit_code=exit_code,
        outcome="clean" if exit_code == 0 else "collector-error",
        snapshot_fresh=observed,
        docker_observed=observed,
        snapshot="snapshot.json",
    )


class FakeCensus:
    exit_code = 0

    def handle(self, request: object) -> ModelLaneCensusRunResult:
        return census_result(FakeCensus.exit_code)


def run_main(
    monkeypatch: pytest.MonkeyPatch,
    census_exit: int,
    outcome: str,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    FakeCensus.exit_code = census_exit

    class FakeStep:
        def handle(self, request: object):
            from omnimarket.nodes.node_compose_project_census_compute.models.model_compose_project_census import (
                ModelComposeProjectCheckResult,
            )

            return ModelComposeProjectCheckResult(outcome=outcome)  # type: ignore[arg-type]

    monkeypatch.setattr(node_main, "HandlerLaneCensusEffect", FakeCensus)
    monkeypatch.setattr(node_main, "_project_handler", lambda: FakeStep())
    code = node_main.main(["--omni-home", "/srv/workspace"])
    return code, capsys.readouterr().out


@pytest.mark.parametrize(
    ("census_exit", "outcome", "expected"),
    [
        (0, "drift", 30),
        (0, "clean", 0),
        (0, "skipped-no-policy", 0),
        (0, "inventory-unobservable", 4),
        (0, "host-unmatched", 5),
        (4, "drift", 4),
        (4, "clean", 4),
        (30, "clean", 30),
        (30, "drift", 30),
    ],
)
def test_main_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    census_exit: int,
    outcome: str,
    expected: int,
) -> None:
    code, out = run_main(monkeypatch, census_exit, outcome, capsys)
    assert code == expected
    last = json.loads(out.strip().splitlines()[-1])
    assert last["compose_project_check"]["outcome"] == outcome


def test_contract_schedule_carries_the_docker_binary_and_the_step_bound() -> None:
    schedule = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())["schedule"]
    assert schedule["docker_binary"]
    assert schedule["project_check_timeout_seconds"] > 0
    without = {k: v for k, v in schedule.items() if k != "docker_binary"}
    with pytest.raises(ValidationError, match="docker_binary"):
        ModelLaneCensusSchedule.model_validate(without)
