"""The composed boot job provisions its Python 3.12 with retries before any uv run.

`ubuntu-latest` is now an image with no Python 3.12, so the first `uv run` in the infra
checkout (`make -C infra up-local`, step "Build the infra runtime image at the pinned ref")
downloaded cpython from github.com inside that step. A 504 or a stalled request there failed the
step in 12 to 120 seconds and, through CI Summary, every open pull request it ran on. The job now
installs the interpreter in its own step that retries and fails loudly when every attempt fails,
and tells uv never to download one later.
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
PROVISION_STEP = "Provision Python 3.12 with retries (the runner image carries none)"
BUILD_STEP = "Build the infra runtime image at the pinned ref"
RENDER_STEP = "Render the composed laptop bundle"
ENV_STEP = "Write the laptop env file and model overlay (make local-env)"
INFRA_CHECKOUT_STEP = "Checkout omnibase_infra at the locked release"


@pytest.fixture
def job() -> dict[str, Any]:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/runtime-boot-composed.yml").read_text()
    )
    job = workflow["jobs"]["composed-runtime-boot"]
    assert isinstance(job, dict)
    return job


def names(job: dict[str, Any]) -> list[str | None]:
    return [s.get("name") for s in job["steps"]]


def provision(job: dict[str, Any]) -> dict[str, Any]:
    matches = [s for s in job["steps"] if s.get("name") == PROVISION_STEP]
    assert len(matches) == 1, f"the job has no single step named {PROVISION_STEP!r}"
    return matches[0]


def fake_uv(tmp_path: Path, fail_first: int) -> tuple[str, Path]:
    """A uv whose `python install` fails the first ``fail_first`` calls, then installs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    counter = tmp_path / "calls"
    counter.write_text("0", encoding="utf-8")
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/bash\n"
        'echo "uv $*" >> "' + str(tmp_path / "log") + '"\n'
        'if [ "$1 $2" = "python install" ]; then\n'
        '  n=$(( $(cat "' + str(counter) + '") + 1 ))\n'
        '  echo "$n" > "' + str(counter) + '"\n'
        '  if [ "$n" -le ' + str(fail_first) + " ]; then\n"
        '    echo "error: Failed to download (504 Gateway Timeout)" >&2\n'
        "    exit 2\n"
        "  fi\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return f"{bin_dir}:{os.environ['PATH']}", counter


def run_provision(job: dict[str, Any], path: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": path,
        "UV_PYTHON": "3.12",
        # No real sleep in a unit test; the seconds are a job setting, read here too.
        "COMPOSED_PYTHON_INSTALL_RETRY_SECONDS": "0",
        "COMPOSED_PYTHON_INSTALL_ATTEMPTS": str(
            job["env"]["COMPOSED_PYTHON_INSTALL_ATTEMPTS"]
        ),
    }
    return subprocess.run(
        ["bash", "-c", provision(job)["run"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.unit
def test_provision_step_comes_before_every_step_that_runs_uv(
    job: dict[str, Any],
) -> None:
    order = names(job)
    assert order.index(INFRA_CHECKOUT_STEP) < order.index(PROVISION_STEP)
    assert order.index(PROVISION_STEP) < order.index(ENV_STEP)
    assert order.index(PROVISION_STEP) < order.index(BUILD_STEP)
    assert order.index(PROVISION_STEP) < order.index(RENDER_STEP)


@pytest.mark.unit
def test_a_later_step_can_never_download_an_interpreter(job: dict[str, Any]) -> None:
    assert job["env"]["UV_PYTHON_DOWNLOADS"] == "never"
    # Only the provisioning step may download, and only by saying so itself.
    assert provision(job)["env"]["UV_PYTHON_DOWNLOADS"] == "automatic"
    others = [s for s in job["steps"] if s.get("name") != PROVISION_STEP]
    assert all(
        s.get("env", {}).get("UV_PYTHON_DOWNLOADS", "never") == "never" for s in others
    )


@pytest.mark.unit
def test_attempt_count_is_a_declared_job_setting_with_a_real_retry(
    job: dict[str, Any],
) -> None:
    attempts = job["env"]["COMPOSED_PYTHON_INSTALL_ATTEMPTS"]
    assert re.fullmatch(r"[0-9]+", str(attempts))
    assert int(attempts) >= 3
    assert re.fullmatch(
        r"[0-9]+", str(job["env"]["COMPOSED_PYTHON_INSTALL_RETRY_SECONDS"])
    )


@pytest.mark.unit
def test_a_download_that_fails_twice_is_retried_and_the_step_passes(
    job: dict[str, Any], tmp_path: Path
) -> None:
    path, counter = fake_uv(tmp_path, fail_first=2)
    result = run_provision(job, path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert counter.read_text(encoding="utf-8").strip() == "3"
    # The read-back: the step asks uv where the interpreter is instead of assuming it.
    assert "uv python find 3.12" in (tmp_path / "log").read_text(encoding="utf-8")


@pytest.mark.unit
def test_injected_break_every_attempt_failing_fails_the_step_loudly(
    job: dict[str, Any], tmp_path: Path
) -> None:
    attempts = int(job["env"]["COMPOSED_PYTHON_INSTALL_ATTEMPTS"])
    path, counter = fake_uv(tmp_path, fail_first=10**6)
    result = run_provision(job, path)
    assert result.returncode == 1
    assert counter.read_text(encoding="utf-8").strip() == str(attempts)
    assert "::error::" in result.stdout + result.stderr
    assert str(attempts) in result.stdout + result.stderr
