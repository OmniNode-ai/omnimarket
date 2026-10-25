# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Execute the composed-boot consumer-group probe step against a stub broker (OMN-21279).

Moved with the step from omnibase_infra tests/ci/test_runtime_boot_catalog_local_probe_omn19496.py
(OMN-19496): the step now lives in this repo's composed-runtime-boot job.

The ``composed-runtime-boot`` job in
``.github/workflows/runtime-boot-composed.yml`` ends with a step that reads
``rpk group list`` and ``rpk group describe`` to prove a live
``node_delegate_skill_orchestrator`` consumer group on the delegate-skill
command topic. Its first CI run (job 107881660050) failed that step with exit
code 141 within 120 ms of starting, while the runtime was healthy and the group
was present: under ``set -euo pipefail`` a reader that stops early (``awk ...
{exit}``, ``head -12``) sends SIGPIPE to the writer still producing output, and
pipefail turns the writer's 141 into the step's exit status.

These tests run the step's own ``run:`` text, unmodified, with ``docker`` and
``sleep`` replaced on PATH by stubs, so the proof is the shipped script and not
a paraphrase of it. The stub broker prints the matching group first and then
far more than one pipe buffer of further rows, which is what makes an early
reader send SIGPIPE deterministically.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_WORKFLOW = (
    Path(__file__).resolve().parents[3]
    / ".github"
    / "workflows"
    / "runtime-boot-composed.yml"
)
_JOB = "composed-runtime-boot"
_STEP = "Delegate-skill command topic has a live consumer group"
_PROJECT = "omnibase-infra-local"
_TOPIC = "onex.cmd.omnimarket.delegate-skill.v1"
_GROUP = f"omnimarket.node_delegate_skill_orchestrator.consume.v1.__t.{_TOPIC}"

#: Rows printed after the interesting ones. At ~60 bytes a row this is several
#: times the 64 KiB Linux pipe buffer, so a reader that exits early always
#: leaves the writer blocked on a closed pipe.
_FILLER_ROWS = 6000

_DOCKER_STUB = """#!/usr/bin/env bash
# Stub of `docker exec [-e VAR]... <broker> rpk group list|describe` for the
# probe step. The broker requires SASL (OMN-20820): a call that does not carry
# the superuser's credentials is refused, as the real one is.
set -u
[ "$1" = "exec" ] || { echo "stub docker: unexpected $*" >&2; exit 2; }
shift
while [ "$1" = "-e" ]; do
  case "$2" in
    *=*) export "$2" ;;
    *) export "$2=$(printenv "$2")" ;;
  esac
  shift 2
done
shift  # the container
if [ "${RPK_USER:-}" != "onex-local" ] || [ "${RPK_PASS:-}" != "$STUB_PASSWORD" ]; then
  echo "unable to request metadata: SASL is required but not provided" >&2
  exit 1
fi
case "$*" in
  "rpk group list")
    echo "BROKER  GROUP  STATE"
    if [ "$STUB_GROUP_PRESENT" = "yes" ]; then
      echo "0  $STUB_GROUP  Stable"
    fi
    i=0
    while [ "$i" -lt "$STUB_FILLER_ROWS" ]; do
      echo "0  omnibase_infra.node_filler_$i.consume.v1.__t.onex.evt.filler.v1  Stable"
      i=$((i + 1))
    done
    ;;
  "rpk group describe $STUB_GROUP")
    echo "GROUP        $STUB_GROUP"
    echo "COORDINATOR  0"
    echo "STATE        Stable"
    echo "BALANCER     range"
    echo "MEMBERS      $STUB_MEMBERS"
    echo "TOTAL-LAG    0"
    echo ""
    echo "TOPIC  PARTITION  CURRENT-OFFSET  LOG-START-OFFSET  LOG-END-OFFSET  LAG"
    i=0
    while [ "$i" -lt "$STUB_FILLER_ROWS" ]; do
      echo "$STUB_TOPIC  $i  0  0  0  0"
      i=$((i + 1))
    done
    ;;
  *)
    echo "stub docker: unexpected exec $*" >&2
    exit 2
    ;;
esac
"""


def _probe_script() -> str:
    workflow: dict[str, Any] = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"][_JOB]["steps"]
    matches = [s for s in steps if s.get("name") == _STEP]
    assert len(matches) == 1, f"expected exactly one step named {_STEP!r}"
    run = matches[0]["run"]
    assert isinstance(run, str)
    return run


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_probe(
    tmp_path: Path,
    *,
    group_present: bool,
    members: int,
    env_text: str = "REDPANDA_SUPERUSER_PASSWORD=stub-secret\n",
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "docker", _DOCKER_STUB)
    # The step retries 30 times with `sleep 10`; a no-op sleep keeps the
    # negative controls fast without changing which branch the script takes.
    _write_executable(bin_dir / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    script = tmp_path / "probe.sh"
    script.write_text(_probe_script(), encoding="utf-8")

    env_file = tmp_path / "local.env"
    env_file.write_text(env_text, encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "LOCAL_ENV_FILE": str(env_file),
        "STUB_PASSWORD": "stub-secret",
        "LOCAL_PROJECT": _PROJECT,
        "DELEGATE_COMMAND_TOPIC": _TOPIC,
        "STUB_GROUP": _GROUP,
        "STUB_TOPIC": _TOPIC,
        "STUB_GROUP_PRESENT": "yes" if group_present else "no",
        "STUB_MEMBERS": str(members),
        "STUB_FILLER_ROWS": str(_FILLER_ROWS),
    }
    # GitHub runs a `run:` block as `bash -e {0}`; the script sets pipefail itself.
    return subprocess.run(
        ["bash", "-e", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_probe_passes_on_a_live_group_despite_early_readers(tmp_path: Path) -> None:
    """The job-107881660050 shape: group present, one member, long listings."""
    result = _run_probe(tmp_path, group_present=True, members=1)
    assert result.returncode == 0, (
        f"exit {result.returncode}\nstdout:\n{result.stdout[-2000:]}\n"
        f"stderr:\n{result.stderr[-2000:]}"
    )
    assert f"live consumer group {_GROUP} (1 member) on {_TOPIC}" in result.stdout


def test_probe_fails_when_no_group_owns_the_topic(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, group_present=False, members=0)
    assert result.returncode == 1, result.stdout[-2000:] + result.stderr[-2000:]
    assert "no live node_delegate_skill_orchestrator consumer group" in result.stdout


def test_probe_fails_when_the_group_has_no_member(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, group_present=True, members=0)
    assert result.returncode == 1, result.stdout[-2000:] + result.stderr[-2000:]
    assert "no live node_delegate_skill_orchestrator consumer group" in result.stdout


def test_probe_authenticates_every_rpk_call_as_the_local_superuser(
    tmp_path: Path,
) -> None:
    """OMN-20820: against a SASL broker an anonymous listing finds no group."""
    result = _run_probe(tmp_path, group_present=True, members=1)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "stub-secret" not in result.stdout + result.stderr


def test_probe_fails_when_the_credentials_are_stripped(tmp_path: Path) -> None:
    """The authenticated probe made anonymous must turn the step red (AC7)."""
    result = _run_probe(
        tmp_path,
        group_present=True,
        members=1,
        env_text="POSTGRES_PASSWORD=unrelated\n",
    )
    assert result.returncode != 0
    assert "REDPANDA_SUPERUSER_PASSWORD is absent" in result.stdout + result.stderr


def test_probe_fails_when_the_superuser_password_is_wrong(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        group_present=True,
        members=1,
        env_text="REDPANDA_SUPERUSER_PASSWORD=wrong\n",
    )
    assert result.returncode != 0
