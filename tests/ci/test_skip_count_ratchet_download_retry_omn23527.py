# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-23527: the Skip Count Ratchet's artifact download retries longer than a burst.

Measured 2026-11-10 10:50Z to 10:56Z: the download of this run's JUnit reports failed
all three attempts inside two minutes, because one GitHub rate-limit burst lasted 6.5
minutes. The step now makes six attempts with a linear backoff (30+60+90+120+150 =
450 seconds), names a download that is still refused by a rate limit or a 5xx in the
job summary, and stays blocking: it exits 0 only when the reports were downloaded.

These tests drive the step's own ``run:`` text under bash with a fake ``gh`` and a fake
``sleep`` first on PATH.
"""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
STEP_NAME = "Download this run's JUnit reports"
BURST_SECONDS = 390  # 6.5 minutes, the measured length of the 2026-11-10 burst

_FAKE_GH = """#!/usr/bin/env bash
# Fake gh: logs each call; behaviour by FAKE_GH_MODE.
echo "$*" >> "$FAKE_GH_LOG"
count_file="$FAKE_GH_LOG.count"
count=$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 ))
echo "$count" > "$count_file"
case "${FAKE_GH_MODE:-ok}" in
  ok) exit 0 ;;
  ratelimit) echo "API rate limit exceeded for installation" >&2; exit 1 ;;
  server) echo "HTTP 503: Service Unavailable" >&2; exit 1 ;;
  nonrate) echo "HTTP 404: Not Found" >&2; exit 1 ;;
  flaky)
    if [ "$count" -le 2 ]; then
      echo "API rate limit exceeded for installation" >&2
      exit 1
    fi
    exit 0 ;;
esac
"""


def _job() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return data["jobs"]["skip-count-ratchet"]


def _step() -> dict[str, Any]:
    (step,) = [s for s in _job()["steps"] if s.get("name") == STEP_NAME]
    return step


def _install(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run(
    tmp_path: Path,
    mode: str,
    run_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    _install(bin_dir, "gh", _FAKE_GH)
    _install(bin_dir, "sleep", '#!/usr/bin/env bash\necho "$*" >> "$FAKE_SLEEP_LOG"\n')
    script = tmp_path / "step.sh"
    script.write_text(run_text or _step()["run"], encoding="utf-8")
    step_env = {k: str(v) for k, v in _step()["env"].items() if "${{" not in str(v)}
    full_env = {
        "PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GH_TOKEN": "dummy",
        "RUN_ID": "1",
        "REPO": "o/r",
        **step_env,
        "FAKE_GH_LOG": str(tmp_path / "gh.log"),
        "FAKE_SLEEP_LOG": str(tmp_path / "sleep.log"),
        "FAKE_GH_MODE": mode,
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        **(env or {}),
    }
    return subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


def _gh_calls(tmp_path: Path) -> int:
    count_file = tmp_path / "gh.log.count"
    return int(count_file.read_text()) if count_file.exists() else 0


def _sleeps(tmp_path: Path) -> list[int]:
    log = tmp_path / "sleep.log"
    return [int(x) for x in log.read_text().split()] if log.exists() else []


def _summary(tmp_path: Path) -> str:
    path = tmp_path / "summary.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _retry_span_seconds(env: dict[str, Any]) -> int:
    attempts = int(env["DOWNLOAD_ATTEMPTS"])
    base = int(env["DOWNLOAD_BACKOFF_SECONDS"])
    return sum(base * n for n in range(1, attempts))


def test_download_succeeds_first_time_with_one_call(tmp_path: Path) -> None:
    result = _run(tmp_path, "ok")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _gh_calls(tmp_path) == 1
    assert _sleeps(tmp_path) == []


def test_download_that_recovers_passes_on_the_third_attempt(tmp_path: Path) -> None:
    result = _run(tmp_path, "flaky")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _gh_calls(tmp_path) == 3


def test_download_refused_by_a_rate_limit_retries_past_a_burst_then_fails_and_says_so(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, "ratelimit")
    assert result.returncode == 1
    assert _gh_calls(tmp_path) == 6
    assert _sleeps(tmp_path) == [30, 60, 90, 120, 150]
    assert sum(_sleeps(tmp_path)) > BURST_SECONDS
    summary = _summary(tmp_path)
    assert "rate limit" in summary
    assert "did not run" in summary
    assert "NOT a finding" in summary
    assert "Failing closed" in result.stdout


def test_download_refused_by_a_server_error_is_named_as_such(tmp_path: Path) -> None:
    result = _run(tmp_path, "server")
    assert result.returncode == 1
    assert "did not run" in _summary(tmp_path)


def test_download_failing_for_another_reason_is_still_retried_and_never_blamed_on_a_rate_limit(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, "nonrate")
    assert result.returncode == 1
    assert _gh_calls(tmp_path) == 6
    assert "rate limit" not in _summary(tmp_path)
    assert "could not download this run's JUnit reports" in result.stdout


def test_the_declared_retry_span_outlasts_the_measured_burst() -> None:
    assert _retry_span_seconds(_step()["env"]) > BURST_SECONDS


def test_span_check_has_a_positive_control() -> None:
    env = dict(_step()["env"])
    env["DOWNLOAD_ATTEMPTS"] = "3"
    env["DOWNLOAD_BACKOFF_SECONDS"] = "10"
    assert _retry_span_seconds(env) <= BURST_SECONDS


def test_step_stays_blocking_and_the_job_bound_is_unchanged() -> None:
    step = _step()
    assert "continue-on-error" not in step
    assert not re.search(r"\|\|\s*true", step["run"])
    assert _job()["timeout-minutes"] == 15
    assert "continue-on-error" not in _job()
