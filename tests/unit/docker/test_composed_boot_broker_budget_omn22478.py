"""OMN-22478: the composed boot job sizes its own broker for the whole composed topic set.

The infra laptop env template gives a broker 2G. At 1 MiB reserved per partition the broker
refused ``onex.snapshot.projection.work.events.v1`` at its partition ceiling and both kernels
crash-looped. The job now writes its own budget into the env file and reads the broker's start
arguments back before it boots the stack.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SIZE_STEP = "Size this job's own broker for the composed topic set (OMN-22478)"
READ_BACK_STEP = "The broker started on the sized budget (read back, not assumed)"
BUILD_STEP = "Build the infra runtime image at the pinned ref"
BOOT_STEP = "Boot the composed laptop profile"
ENV_STEP = "Write the laptop env file and model overlay (make local-env)"
MIN_BUDGET_GIB = 4
TEMPLATE_ENV = (
    "POSTGRES_PASSWORD=x\nREDPANDA_ADVERTISE_HOST=localhost\nREDPANDA_MEMORY=2G\n"
    "ONEX_LOCAL_BIFROST_OVERLAY=/tmp/o.yaml\n"
)


@pytest.fixture
def job() -> dict[str, Any]:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/runtime-boot-composed.yml").read_text()
    )
    job = workflow["jobs"]["composed-runtime-boot"]
    assert isinstance(job, dict)
    return job


def step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [s for s in job["steps"] if s.get("name") == name]
    assert len(matches) == 1, name
    return matches[0]


def index(job: dict[str, Any], name: str) -> int:
    return [s.get("name") for s in job["steps"]].index(name)


def run(
    script: str, env: dict[str, str], path: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        env={"PATH": path or os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.unit
def test_budget_is_at_least_the_infra_ci_broker_floor(job: dict[str, Any]) -> None:
    budget = job["env"]["COMPOSED_BROKER_MEMORY"]
    match = re.fullmatch(r"([0-9]+)G", budget)
    assert match, budget
    assert int(match.group(1)) >= MIN_BUDGET_GIB


@pytest.mark.unit
def test_budget_is_written_before_the_stack_is_built_and_read_back_before_boot(
    job: dict[str, Any],
) -> None:
    assert (
        index(job, ENV_STEP)
        < index(job, SIZE_STEP)
        < index(job, BUILD_STEP)
        < index(job, READ_BACK_STEP)
        < index(job, BOOT_STEP)
    )


@pytest.mark.unit
def test_sizing_step_replaces_the_template_line_and_keeps_the_rest(
    job: dict[str, Any], tmp_path: Path
) -> None:
    env_file = tmp_path / "local.env"
    env_file.write_text(TEMPLATE_ENV, encoding="utf-8")
    result = run(
        step(job, SIZE_STEP)["run"],
        {
            "LOCAL_ENV_FILE": str(env_file),
            "COMPOSED_BROKER_MEMORY": job["env"]["COMPOSED_BROKER_MEMORY"],
        },
    )
    assert result.returncode == 0, result.stderr
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines.count(f"REDPANDA_MEMORY={job['env']['COMPOSED_BROKER_MEMORY']}") == 1
    assert not any(line == "REDPANDA_MEMORY=2G" for line in lines)
    assert "POSTGRES_PASSWORD=x" in lines
    assert "ONEX_LOCAL_BIFROST_OVERLAY=/tmp/o.yaml" in lines


@pytest.mark.unit
def test_injected_break_template_without_the_dial_fails_the_sizing_step(
    job: dict[str, Any], tmp_path: Path
) -> None:
    env_file = tmp_path / "local.env"
    env_file.write_text("POSTGRES_PASSWORD=x\n", encoding="utf-8")
    result = run(
        step(job, SIZE_STEP)["run"],
        {
            "LOCAL_ENV_FILE": str(env_file),
            "COMPOSED_BROKER_MEMORY": job["env"]["COMPOSED_BROKER_MEMORY"],
        },
    )
    assert result.returncode == 1
    assert "declares no REDPANDA_MEMORY line" in result.stdout
    assert "REDPANDA_MEMORY" not in env_file.read_text(encoding="utf-8")


def fake_docker(tmp_path: Path, start_arguments: list[str]) -> str:
    """A docker that answers the inspect template with the given broker start arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/bash\nfor argument in "
        + " ".join(f"'{a}'" for a in start_arguments)
        + '; do printf "%s " "$argument"; done\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return f"{bin_dir}:{os.environ['PATH']}"


@pytest.mark.unit
@pytest.mark.parametrize(
    "arguments",
    [["redpanda", "start", "--memory 6G", "--smp 1"], ["redpanda", "--memory 6G"]],
)
def test_read_back_passes_when_the_broker_started_on_the_budget(
    job: dict[str, Any], tmp_path: Path, arguments: list[str]
) -> None:
    result = run(
        step(job, READ_BACK_STEP)["run"],
        {
            "LOCAL_PROJECT": "omnibase-infra-local",
            "COMPOSED_BROKER_MEMORY": job["env"]["COMPOSED_BROKER_MEMORY"],
        },
        path=fake_docker(tmp_path, arguments),
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    "arguments",
    [
        ["redpanda", "start", "--memory 2G", "--smp 1"],
        ["redpanda", "start", "--memory 60G"],
        ["redpanda", "start", "--smp 1"],
    ],
)
def test_injected_break_broker_on_another_budget_fails_the_read_back(
    job: dict[str, Any], tmp_path: Path, arguments: list[str]
) -> None:
    result = run(
        step(job, READ_BACK_STEP)["run"],
        {
            "LOCAL_PROJECT": "omnibase-infra-local",
            "COMPOSED_BROKER_MEMORY": job["env"]["COMPOSED_BROKER_MEMORY"],
        },
        path=fake_docker(tmp_path, arguments),
    )
    assert result.returncode == 1
    assert "did not start with --memory" in result.stdout
