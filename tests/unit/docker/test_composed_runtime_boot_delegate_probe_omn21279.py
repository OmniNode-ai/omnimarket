# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Execute the composed-boot consumer-group probe step against a stub broker (OMN-21279).

Moved with the step from omnibase_infra tests/ci/test_runtime_boot_catalog_local_probe_omn19496.py
(OMN-19496): the step now lives in this repo's composed-runtime-boot job.

The ``composed-runtime-boot`` job in
``.github/workflows/runtime-boot-composed.yml`` has a step that reads
``rpk group list`` and ``rpk group describe`` to prove a live
``node_delegate_skill_orchestrator`` consumer group on the delegate-skill
command topic. Its first CI run (job 107881660050) failed that step with exit
code 141 within 120 ms of starting, while the runtime was healthy and the group
was present: under ``set -euo pipefail`` a reader that stops early (``awk ...
{exit}``, ``head -12``) sends SIGPIPE to the writer still producing output, and
pipefail turns the writer's 141 into the step's exit status.

OMN-18087 (omnibase_infra#5426) made the runtime join a contract's consumer
group at boot only when one of its topics already holds a record; every other
contract joins when its first request arrives. On a fresh boot the command
topic is empty, so the step validates one real delegate-skill request with the
kernel's own model, publishes it, asserts the group it started, and then
requires that request's own terminal, a typed refusal before dispatch. The
stub models that: with ``STUB_JOIN=demand`` the group is listed only after a
record was produced, and the terminal topics carry only what ``STUB_TERMINAL``
says the orchestrator answered for the produced request's correlation id.

These tests run the step's own ``run:`` text, unmodified, with ``docker``,
``sleep`` and the kernel's model check replaced on PATH by stubs, so the proof
is the shipped script and not a paraphrase of it. The stub broker prints the
matching rows first and then far more than one pipe buffer of further rows,
which is what makes an early reader send SIGPIPE deterministically.
"""

from __future__ import annotations

import json
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
_COMPLETED = "onex.evt.omnimarket.delegate-skill-completed.v1"
_FAILED = "onex.evt.omnimarket.delegate-skill-failed.v1"
_GROUP = f"omnimarket.node_delegate_skill_orchestrator.consume.v1.__t.{_TOPIC}"

#: Rows printed after the interesting ones. At ~60 bytes a row this is several
#: times the 64 KiB Linux pipe buffer, so a reader that exits early always
#: leaves the writer blocked on a closed pipe.
_FILLER_ROWS = 6000

#: The request fields ModelDelegateSkillRequest requires, and the ones this
#: probe must set for a refusal before dispatch. The stub's model check
#: refuses anything else, as the real model (extra="forbid") does.
_REQUEST_KEYS = {
    "prompt",
    "task_type",
    "source",
    "correlation_id",
    "requested_timeout_seconds",
    "provenance",
}

_MODEL_STUB = """#!/usr/bin/env python3
# Stub of the kernel's ModelDelegateSkillRequest.model_validate_json(stdin).
import json, os, sys, uuid
record = json.loads(sys.stdin.read())
assert set(record) == set(json.loads(os.environ["STUB_REQUEST_KEYS"])), sorted(record)
uuid.UUID(record["correlation_id"])
assert record["source"] == "external-client"
assert record["provenance"]["traffic_class"] == "synthetic"
assert record["provenance"]["source_surface"]
assert record["requested_timeout_seconds"] > 240
if os.environ["STUB_MODEL_REJECTS"] == "yes":
    sys.exit("1 validation error for ModelDelegateSkillRequest")
"""

_DOCKER_STUB = """#!/usr/bin/env bash
# Stub of `docker exec [-i] [-e VAR]... <container> ...` for the probe step:
# the kernel's model check in the runtime container, and `rpk group
# list|describe`, `rpk topic produce` and `rpk topic consume` in the broker.
# The broker requires SASL (OMN-20820): a call that does not carry the
# superuser's credentials is refused, as the real one is. Every call is
# appended to $STUB_CALLS.
set -u
[ "$1" = "exec" ] || { echo "stub docker: unexpected $*" >&2; exit 2; }
shift
stdin_attached=no
while [ "$1" = "-e" ] || [ "$1" = "-i" ]; do
  if [ "$1" = "-i" ]; then
    stdin_attached=yes
    shift
    continue
  fi
  case "$2" in
    *=*) export "$2" ;;
    *) export "$2=$(printenv "$2")" ;;
  esac
  shift 2
done
container="$1"
shift
args="$*"
echo "$container ${args//$'\\n'/ }" >> "$STUB_CALLS"
if [ "$container" = "$STUB_PROJECT-omninode-runtime" ]; then
  [ "$1" = "/app/.venv/bin/python" ] || { echo "stub docker: unexpected kernel exec $*" >&2; exit 2; }
  [ "$stdin_attached" = "yes" ] || { echo "stub docker: model check without -i reads nothing" >&2; exit 2; }
  exec stub-model-check
fi
[ "$container" = "$STUB_PROJECT-redpanda" ] || { echo "stub docker: unexpected container $container" >&2; exit 2; }
if [ "${RPK_USER:-}" != "onex-local" ] || [ "${RPK_PASS:-}" != "$STUB_PASSWORD" ]; then
  echo "unable to request metadata: SASL is required but not provided" >&2
  exit 1
fi
# A deferred contract joins only once a record reached its topic (OMN-18087).
group_listed=no
if [ "$STUB_GROUP_PRESENT" = "yes" ]; then
  if [ "$STUB_JOIN" = "boot" ] || [ -s "$STUB_PRODUCED" ]; then
    group_listed=yes
  fi
fi
probe_id=""
if [ -s "$STUB_PRODUCED" ]; then
  probe_id="$(python3 -c 'import json,sys; print(json.loads(open(sys.argv[1]).readline())["correlation_id"])' "$STUB_PRODUCED")"
fi
filler() {
  i=0
  while [ "$i" -lt "$STUB_FILLER_ROWS" ]; do
    echo "{\\"correlation_id\\":\\"00000000-0000-4000-8000-$(printf '%012d' "$i")\\",\\"status\\":\\"failed\\"}"
    i=$((i + 1))
  done
}
case "$*" in
  "rpk topic produce $STUB_TOPIC -k composed-runtime-boot-probe")
    [ "$stdin_attached" = "yes" ] || { echo "stub docker: produce without -i reads no record" >&2; exit 2; }
    if [ "$STUB_PRODUCE_FAILS" = "yes" ]; then
      echo "unable to produce record: NOT_LEADER_FOR_PARTITION" >&2
      exit 1
    fi
    record="$(cat)"
    [ -n "$record" ] || { echo "stub docker: empty record" >&2; exit 2; }
    printf '%s\\n' "$record" >> "$STUB_PRODUCED"
    echo "Produced to partition 0 at offset 0 with timestamp 1793210000000."
    ;;
  "rpk topic consume $STUB_FAILED -o start:end -f %v\\n")
    if [ -n "$probe_id" ]; then
      case "$STUB_TERMINAL" in
        refused) echo "{\\"correlation_id\\":\\"$probe_id\\",\\"status\\":\\"failed\\",\\"terminal_failure_cause\\":\\"refused_before_dispatch\\",\\"attempts_count\\":0}" ;;
        provider_error) echo "{\\"correlation_id\\":\\"$probe_id\\",\\"status\\":\\"failed\\",\\"terminal_failure_cause\\":\\"provider_error\\"}" ;;
        other_request) echo "{\\"correlation_id\\":\\"11111111-1111-4111-8111-111111111111\\",\\"terminal_failure_cause\\":\\"refused_before_dispatch\\"}" ;;
      esac
    fi
    filler
    ;;
  "rpk topic consume $STUB_COMPLETED -o start:end -f %v\\n")
    if [ -n "$probe_id" ] && [ "$STUB_TERMINAL" = "completed" ]; then
      echo "{\\"correlation_id\\":\\"$probe_id\\",\\"status\\":\\"completed\\"}"
    fi
    filler
    ;;
  "rpk group list")
    echo "BROKER  GROUP  STATE"
    if [ "$group_listed" = "yes" ]; then
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
    join: str = "boot",
    terminal: str = "refused",
    produce_fails: bool = False,
    model_rejects: bool = False,
    terminal_topics: bool = True,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "docker", _DOCKER_STUB)
    _write_executable(bin_dir / "stub-model-check", _MODEL_STUB)
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
        "STUB_PROJECT": _PROJECT,
        "DELEGATE_COMMAND_TOPIC": _TOPIC,
        "STUB_GROUP": _GROUP,
        "STUB_TOPIC": _TOPIC,
        "STUB_COMPLETED": _COMPLETED,
        "STUB_FAILED": _FAILED,
        "STUB_GROUP_PRESENT": "yes" if group_present else "no",
        "STUB_MEMBERS": str(members),
        "STUB_FILLER_ROWS": str(_FILLER_ROWS),
        "STUB_JOIN": join,
        "STUB_TERMINAL": terminal,
        "STUB_PRODUCE_FAILS": "yes" if produce_fails else "no",
        "STUB_MODEL_REJECTS": "yes" if model_rejects else "no",
        "STUB_REQUEST_KEYS": json.dumps(sorted(_REQUEST_KEYS)),
        "STUB_PRODUCED": str(tmp_path / "produced.txt"),
        "STUB_CALLS": str(tmp_path / "calls.txt"),
    }
    if terminal_topics:
        env["DELEGATE_COMPLETED_TOPIC"] = _COMPLETED
        env["DELEGATE_FAILED_TOPIC"] = _FAILED
    # GitHub runs a `run:` block as `bash -e {0}`; the script sets pipefail itself.
    return subprocess.run(
        ["bash", "-e", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _calls(tmp_path: Path) -> list[str]:
    path = tmp_path / "calls.txt"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _produced(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "produced.txt"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _out(result: subprocess.CompletedProcess[str]) -> str:
    return f"exit {result.returncode}\nstdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-2000:]}"


def test_probe_passes_on_a_live_group_despite_early_readers(tmp_path: Path) -> None:
    """The job-107881660050 shape: group present, one member, long listings."""
    result = _run_probe(tmp_path, group_present=True, members=1)
    assert result.returncode == 0, _out(result)
    assert f"live consumer group {_GROUP} (1 member) on {_TOPIC}" in result.stdout
    assert "typed refusal for probe" in result.stdout


def test_probe_fails_when_no_group_owns_the_topic(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, group_present=False, members=0)
    assert result.returncode == 1, _out(result)
    assert "no live node_delegate_skill_orchestrator consumer group" in result.stdout


def test_probe_fails_when_the_group_has_no_member(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, group_present=True, members=0)
    assert result.returncode == 1, _out(result)
    assert "no live node_delegate_skill_orchestrator consumer group" in result.stdout


def test_probe_authenticates_every_rpk_call_as_the_local_superuser(
    tmp_path: Path,
) -> None:
    """OMN-20820: against a SASL broker an anonymous listing finds no group."""
    result = _run_probe(tmp_path, group_present=True, members=1)
    assert result.returncode == 0, _out(result)
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


def test_probe_starts_a_group_deferred_until_the_first_request(
    tmp_path: Path,
) -> None:
    """OMN-18087: the empty command topic's group joins only on a record.

    Against the step before the probe this fails: nothing is produced, so the
    deferred group never appears (job 123579641012).
    """
    result = _run_probe(tmp_path, group_present=True, members=1, join="demand")
    assert result.returncode == 0, _out(result)
    assert f"live consumer group {_GROUP} (1 member) on {_TOPIC}" in result.stdout
    produced = _produced(tmp_path)
    assert len(produced) == 1, "exactly one probe request per run"
    assert set(produced[0]) == _REQUEST_KEYS
    probe_id = produced[0]["correlation_id"]
    assert f"typed refusal for probe {probe_id} on {_FAILED}" in result.stdout
    calls = _calls(tmp_path)
    assert calls[0].startswith(
        f"{_PROJECT}-omninode-runtime /app/.venv/bin/python -c "
    ), "the kernel's model checks the request before anything is published"
    assert calls[1] == (
        f"{_PROJECT}-redpanda rpk topic produce {_TOPIC} -k composed-runtime-boot-probe"
    )


def test_probe_fails_when_no_orchestrator_joins_on_demand(tmp_path: Path) -> None:
    """The request lands but no orchestrator is wired to answer it."""
    result = _run_probe(tmp_path, group_present=False, members=0, join="demand")
    assert result.returncode == 1, _out(result)
    assert len(_produced(tmp_path)) == 1
    assert "no live node_delegate_skill_orchestrator consumer group" in result.stdout


def test_probe_fails_when_the_group_joins_but_the_request_gets_no_terminal(
    tmp_path: Path,
) -> None:
    """A group that joined but lost its first request is not a pass."""
    result = _run_probe(
        tmp_path, group_present=True, members=1, join="demand", terminal="none"
    )
    assert result.returncode == 1, _out(result)
    assert f"live consumer group {_GROUP}" in result.stdout
    assert "no terminal for probe" in result.stdout


def test_probe_ignores_a_terminal_for_another_request(tmp_path: Path) -> None:
    """A refusal already on the topic for an earlier request proves nothing."""
    result = _run_probe(
        tmp_path,
        group_present=True,
        members=1,
        join="demand",
        terminal="other_request",
    )
    assert result.returncode == 1, _out(result)
    assert "no terminal for probe" in result.stdout


def test_probe_fails_when_the_request_was_dispatched(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path, group_present=True, members=1, join="demand", terminal="completed"
    )
    assert result.returncode == 1, _out(result)
    assert "was dispatched (completed terminal)" in result.stdout


def test_probe_fails_on_a_failure_other_than_the_refusal(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        group_present=True,
        members=1,
        join="demand",
        terminal="provider_error",
    )
    assert result.returncode == 1, _out(result)
    assert "failed without a refusal before dispatch" in result.stdout


def test_probe_fails_when_the_probe_cannot_be_published(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path, group_present=True, members=1, join="demand", produce_fails=True
    )
    assert result.returncode == 1, _out(result)
    assert "could not publish the demand-start probe" in result.stdout
    assert not any(" rpk group " in call for call in _calls(tmp_path))


def test_probe_publishes_nothing_the_kernel_model_refuses(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path, group_present=True, members=1, join="demand", model_rejects=True
    )
    assert result.returncode == 1, _out(result)
    assert "is not a valid ModelDelegateSkillRequest" in result.stdout
    assert _produced(tmp_path) == []


def test_probe_fails_without_the_terminal_topics(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, group_present=True, members=1, terminal_topics=False)
    assert result.returncode == 1, _out(result)
    assert "terminal topics were not read from the contract" in result.stdout
    assert _calls(tmp_path) == []
