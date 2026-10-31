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

#: Records each terminal-topic read prints besides the interesting one. The
#: step parses every record, so this stays small enough that a 30-poll wait
#: ends well inside the test timeout.
_CONSUME_FILLER_ROWS = 200

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
# the kernel's model check and terminal reader in the runtime container, and
# `rpk group list|describe` and `rpk topic produce` in the broker.
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
  [ "$1" = "/app/.venv/bin/python" ] && [ "$2" = "-c" ] || { echo "stub docker: unexpected kernel exec $*" >&2; exit 2; }
  case "$3" in
    *ModelDelegateSkillRequest*)
      [ "$stdin_attached" = "yes" ] || { echo "stub docker: model check without -i reads nothing" >&2; exit 2; }
      exec stub-model-check
      ;;
    *AIOKafkaConsumer*)
      # The step's own reader, run against the fake aiokafka on PYTHONPATH.
      PYTHONPATH="$STUB_FAKES" exec python3 -c "$3"
      ;;
  esac
  echo "stub docker: unexpected kernel program" >&2
  exit 2
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

#: A fake of the aiokafka surface the step's terminal reader uses, with the
#: broker's semantics: partitions come from the admin client, a partition's
#: offsets may be unavailable, the end offset is taken when asked (under
#: read_committed it is the last stable offset, before any still-open
#: transaction), a fetch returns the records from the position to that end and
#: moves the position there, skipping compaction holes and (under
#: read_committed) aborted records, a lost start offset either raises
#: (auto_offset_reset="none") or silently jumps to the end (the "latest"
#: default), and records can arrive after the end offsets were read. The
#: terminal topics' contents follow STUB_TERMINAL for the produced request.
#: A record is bytes, None (a compaction hole) or ("aborted", bytes).
_FAKE_AIOKAFKA = """
import json
import os
from collections import namedtuple

TopicPartition = namedtuple("TopicPartition", ["topic", "partition"])
Record = namedtuple("Record", ["offset", "value"])


def _probe_id():
    path = os.environ["STUB_PRODUCED"]
    if not os.path.exists(path) or not os.path.getsize(path):
        return None
    with open(path) as fh:
        return json.loads(fh.readline())["correlation_id"]


def _rec(**fields):
    return json.dumps(fields).encode()


def _filler():
    return [
        _rec(correlation_id=f"00000000-0000-4000-8000-{i:012d}", status="failed")
        for i in range(int(os.environ["STUB_CONSUME_FILLER_ROWS"]))
    ]


def _broker():
    probe = _probe_id()
    other = "11111111-1111-4111-8111-111111111111"
    refusal = dict(status="failed", terminal_failure_cause="refused_before_dispatch", attempts_count=0)
    scenario = os.environ["STUB_TERMINAL"] if probe else "none"
    failed = {0: {"begin": 0, "records": []}}
    completed = {0: {"begin": 0, "records": _filler()}}
    p0 = failed[0]["records"]
    late = []
    if scenario == "refused":
        p0.append(_rec(correlation_id=probe, **refusal))
    elif scenario == "provider_error":
        p0.append(_rec(correlation_id=probe, status="failed", terminal_failure_cause="provider_error"))
    elif scenario == "other_request":
        p0.append(_rec(correlation_id=other, **refusal))
    elif scenario == "provider_error_mentions_refusal":
        p0.append(_rec(correlation_id=probe, status="failed", terminal_failure_cause="provider_error", error_message="not refused_before_dispatch"))
    elif scenario == "refused_with_attempt":
        p0.append(_rec(correlation_id=probe, status="failed", terminal_failure_cause="refused_before_dispatch", attempts_count=1))
    elif scenario == "refused_enveloped":
        p0.append(_rec(envelope_id="e1", correlation_id=probe, payload=dict(correlation_id=probe, **refusal)))
    elif scenario == "refused_enveloped_conflict":
        p0.append(_rec(envelope_id="e1", correlation_id=probe, payload=dict(correlation_id=other, **refusal)))
    elif scenario == "refused_then_provider_error":
        p0.append(_rec(correlation_id=probe, **refusal))
        p0.append(_rec(correlation_id=probe, status="failed", terminal_failure_cause="provider_error", attempts_count=1))
    elif scenario == "id_in_text_only":
        p0.append(_rec(correlation_id="22222222-2222-4222-8222-222222222222", error_message=f"see {probe}", **refusal))
    elif scenario == "completed":
        completed[0]["records"].append(_rec(correlation_id=probe, status="completed"))
    elif scenario == "unavailable_partition":
        p0.append(_rec(correlation_id=probe, **refusal))
        failed[1] = {"unavailable": True, "records": []}
    elif scenario == "refusal_two_partitions":
        failed[1] = {"begin": 0, "records": [_rec(correlation_id=probe, **refusal)]}
    elif scenario == "late_refusal_hides_other_partition":
        p0.append(_rec(correlation_id=probe, **refusal))
        failed[1] = {"begin": 0, "records": [_rec(correlation_id=probe, status="failed", terminal_failure_cause="provider_error", attempts_count=1)]}
        late.append((0, _rec(correlation_id=probe, **refusal)))
    elif scenario == "short_read":
        p0.append(_rec(correlation_id=probe, **refusal))
        failed[0]["end_extra"] = 1
    elif scenario == "aborted_refusal":
        p0.append(("aborted", _rec(correlation_id=probe, **refusal)))
    elif scenario == "open_transaction_refusal":
        p0.append(_rec(correlation_id=other, **refusal))
        p0.append(_rec(correlation_id=probe, **refusal))
        failed[0]["open_from"] = 1
    elif scenario == "aborted_batch_then_refusal":
        p0.append(("aborted", _rec(correlation_id=probe, status="failed", terminal_failure_cause="provider_error", attempts_count=1)))
        p0.append(_rec(correlation_id=probe, **refusal))
    elif scenario == "offset_reset_hides_contradiction":
        p0.append(_rec(correlation_id=probe, **refusal))
        failed[1] = {"begin": 0, "out_of_range": True, "records": [_rec(correlation_id=probe, status="failed", terminal_failure_cause="provider_error", attempts_count=1)]}
    elif scenario == "post_snapshot_contradiction":
        p0.append(_rec(correlation_id=probe, **refusal))
        late.append((0, _rec(correlation_id=probe, status="failed", terminal_failure_cause="provider_error", attempts_count=1)))
    elif scenario == "compaction_hole":
        p0.append(_rec(correlation_id=probe, **refusal))
        p0.append(None)
    elif scenario == "nonzero_log_start":
        p0.append(_rec(correlation_id=probe, **refusal))
        failed[0]["begin"] = 100
    failed[0]["records"] = _filler() + p0
    if "open_from" in failed[0]:
        failed[0]["open_from"] += len(_filler())
    topics = {os.environ["STUB_FAILED"]: failed}
    if os.environ["STUB_COMPLETED_READ_FAILS"] != "yes":
        topics[os.environ["STUB_COMPLETED"]] = completed
    return topics, late


_TOPICS, _LATE = _broker()


def _partition(tp):
    return _TOPICS[tp.topic][tp.partition]


def _log_end(part):
    return part["begin"] + len(part["records"])


class AIOKafkaConsumer:
    def __init__(
        self,
        *,
        bootstrap_servers,
        group_id,
        enable_auto_commit,
        isolation_level="read_uncommitted",
        auto_offset_reset="latest",
        **auth,
    ):
        assert bootstrap_servers == os.environ["KAFKA_BOOTSTRAP_SERVERS"]
        assert group_id is None and enable_auto_commit is False
        self._committed_only = isolation_level == "read_committed"
        self._reset = auto_offset_reset
        self._positions = {}
        self._fetched = False

    async def start(self):
        pass

    async def stop(self):
        pass

    def assign(self, tps):
        self._positions = {tp: None for tp in tps}

    async def beginning_offsets(self, tps):
        return {tp: _partition(tp)["begin"] for tp in tps if not _partition(tp).get("unavailable")}

    def _end(self, part):
        if self._committed_only and "open_from" in part:
            return part["begin"] + part["open_from"]
        return _log_end(part)

    async def end_offsets(self, tps):
        return {
            tp: self._end(_partition(tp)) + _partition(tp).get("end_extra", 0)
            for tp in tps
            if not _partition(tp).get("unavailable")
        }

    def seek(self, tp, offset):
        self._positions[tp] = offset

    async def getmany(self, *tps, timeout_ms=0):
        # Late records land on the failed topic after its end offsets were
        # read: at the first fetch from that topic.
        if not self._fetched and any(tp.topic == os.environ["STUB_FAILED"] for tp in tps):
            self._fetched = True
            failed = _TOPICS[os.environ["STUB_FAILED"]]
            for partition, value in _LATE:
                failed[partition]["records"].append(value)
        out = {}
        # One partition per fetch, lowest first, as a broker may answer.
        for tp in sorted(tps):
            part = _partition(tp)
            pos = self._positions[tp]
            end = self._end(part)
            if part.get("out_of_range") and pos < end:
                if self._reset == "none":
                    raise RuntimeError("OffsetOutOfRangeError: the captured start offset is gone")
                self._positions[tp] = end
                continue
            if pos < end:
                got = []
                for off in range(pos, end):
                    value = part["records"][off - part["begin"]]
                    if value is None:
                        continue
                    if isinstance(value, tuple):
                        if self._committed_only:
                            continue
                        value = value[1]
                    got.append(Record(off, value))
                out[tp] = got
                self._positions[tp] = end
                break
        return out

    async def position(self, tp):
        return self._positions[tp]
"""

_FAKE_AIOKAFKA_ADMIN = """
import os

from aiokafka import _TOPICS


class AIOKafkaAdminClient:
    def __init__(self, *, bootstrap_servers, **auth):
        assert bootstrap_servers == os.environ["KAFKA_BOOTSTRAP_SERVERS"]

    async def start(self):
        pass

    async def close(self):
        pass

    async def describe_topics(self, topics):
        out = []
        for topic in topics:
            if topic not in _TOPICS:
                out.append({"topic": topic, "error_code": 3, "partitions": []})
            else:
                out.append({"topic": topic, "error_code": 0, "partitions": [{"partition": p} for p in sorted(_TOPICS[topic])]})
        return out
"""


def _write_fakes(root: Path) -> None:
    (root / "aiokafka").mkdir(parents=True)
    (root / "aiokafka" / "__init__.py").write_text(_FAKE_AIOKAFKA, encoding="utf-8")
    (root / "aiokafka" / "structs.py").write_text(
        "from aiokafka import TopicPartition\n", encoding="utf-8"
    )
    (root / "aiokafka" / "admin.py").write_text(_FAKE_AIOKAFKA_ADMIN, encoding="utf-8")
    auth = root / "omnibase_infra" / "event_bus"
    auth.mkdir(parents=True)
    (root / "omnibase_infra" / "__init__.py").write_text("", encoding="utf-8")
    (auth / "__init__.py").write_text("", encoding="utf-8")
    (auth / "kafka_auth.py").write_text(
        "def build_aiokafka_auth_kwargs_from_env():\n    return {}\n", encoding="utf-8"
    )


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
    completed_read_fails: bool = False,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "docker", _DOCKER_STUB)
    _write_executable(bin_dir / "stub-model-check", _MODEL_STUB)
    _write_fakes(tmp_path / "fakes")
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
        "STUB_CONSUME_FILLER_ROWS": str(_CONSUME_FILLER_ROWS),
        "STUB_COMPLETED_READ_FAILS": "yes" if completed_read_fails else "no",
        "STUB_JOIN": join,
        "STUB_TERMINAL": terminal,
        "STUB_PRODUCE_FAILS": "yes" if produce_fails else "no",
        "STUB_MODEL_REJECTS": "yes" if model_rejects else "no",
        "STUB_REQUEST_KEYS": json.dumps(sorted(_REQUEST_KEYS)),
        "STUB_PRODUCED": str(tmp_path / "produced.txt"),
        "STUB_CALLS": str(tmp_path / "calls.txt"),
        "STUB_FAKES": str(tmp_path / "fakes"),
        "KAFKA_BOOTSTRAP_SERVERS": "redpanda:9092",
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


def test_probe_fails_when_only_the_error_text_names_the_refusal(
    tmp_path: Path,
) -> None:
    """Codex 19:23Z: a provider_error whose message mentions the refusal is red."""
    result = _run_probe(
        tmp_path,
        group_present=True,
        members=1,
        join="demand",
        terminal="provider_error_mentions_refusal",
    )
    assert result.returncode == 1, _out(result)
    assert "failed without a refusal before dispatch" in result.stdout
    assert "terminal_failure_cause=provider_error" in result.stdout


def test_probe_fails_on_a_refusal_that_made_an_attempt(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        group_present=True,
        members=1,
        join="demand",
        terminal="refused_with_attempt",
    )
    assert result.returncode == 1, _out(result)
    assert "failed without a refusal before dispatch" in result.stdout


def test_probe_reads_the_refusal_inside_an_envelope(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        group_present=True,
        members=1,
        join="demand",
        terminal="refused_enveloped",
    )
    assert result.returncode == 0, _out(result)
    assert "typed refusal for probe" in result.stdout


def test_probe_ignores_its_id_appearing_only_in_another_terminals_text(
    tmp_path: Path,
) -> None:
    result = _run_probe(
        tmp_path,
        group_present=True,
        members=1,
        join="demand",
        terminal="id_in_text_only",
    )
    assert result.returncode == 1, _out(result)
    assert "no terminal for probe" in result.stdout


def test_probe_does_not_pass_while_the_completed_topic_cannot_be_read(
    tmp_path: Path,
) -> None:
    """Codex 19:27Z: an unread completed topic is not an empty one.

    The refusal is on the failed topic, but whether the same request was also
    dispatched is unknown, so the step must not pass.
    """
    result = _run_probe(
        tmp_path,
        group_present=True,
        members=1,
        join="demand",
        completed_read_fails=True,
    )
    assert result.returncode == 1, _out(result)
    assert f"read failure: {_COMPLETED} cannot be described (error 3)" in result.stdout
    assert "typed refusal for probe" not in result.stdout


def test_probe_rejects_an_envelope_whose_payload_names_another_request(
    tmp_path: Path,
) -> None:
    """Codex 19:38Z: the payload's own correlation id must be the probe's."""
    result = _run_probe(
        tmp_path,
        group_present=True,
        members=1,
        join="demand",
        terminal="refused_enveloped_conflict",
    )
    assert result.returncode == 1, _out(result)
    assert "identity_conflict=True" in result.stdout
    assert "failed without a refusal before dispatch" in result.stdout


def test_probe_reads_every_terminal_for_the_request_before_deciding(
    tmp_path: Path,
) -> None:
    """Codex 19:38Z: a refusal followed by a provider_error for the same id is red."""
    result = _run_probe(
        tmp_path,
        group_present=True,
        members=1,
        join="demand",
        terminal="refused_then_provider_error",
    )
    assert result.returncode == 1, _out(result)
    assert "terminal_failure_cause=provider_error" in result.stdout
    assert "failed without a refusal before dispatch" in result.stdout


def test_probe_fails_when_a_terminal_partition_has_no_offsets(tmp_path: Path) -> None:
    """Codex 20:27Z: one readable empty partition does not make the topic read."""
    result = _run_probe(
        tmp_path,
        group_present=True,
        members=1,
        join="demand",
        terminal="unavailable_partition",
    )
    assert result.returncode == 1, _out(result)
    assert "[1] offsets unavailable" in result.stdout
    assert "typed refusal for probe" not in result.stdout


def test_probe_reads_every_partition_to_its_own_end(tmp_path: Path) -> None:
    """Codex 20:27Z: a late refusal on one partition cannot hide another's failure."""
    result = _run_probe(
        tmp_path,
        group_present=True,
        members=1,
        join="demand",
        terminal="late_refusal_hides_other_partition",
    )
    assert result.returncode == 1, _out(result)
    assert "terminal_failure_cause=provider_error" in result.stdout
    assert "failed without a refusal before dispatch" in result.stdout


def test_probe_rejects_a_read_that_stops_short_of_the_end_offset(
    tmp_path: Path,
) -> None:
    """Codex 20:27Z: fewer records than the end offset promises is a failed read."""
    result = _run_probe(
        tmp_path, group_present=True, members=1, join="demand", terminal="short_read"
    )
    assert result.returncode == 1, _out(result)
    assert "read stopped short" in result.stdout
    assert "typed refusal for probe" not in result.stdout


def test_probe_reads_a_partition_whose_log_starts_above_zero(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        group_present=True,
        members=1,
        join="demand",
        terminal="nonzero_log_start",
    )
    assert result.returncode == 0, _out(result)
    assert "typed refusal for probe" in result.stdout


def test_probe_finds_the_refusal_on_a_second_partition(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        group_present=True,
        members=1,
        join="demand",
        terminal="refusal_two_partitions",
    )
    assert result.returncode == 0, _out(result)
    assert "typed refusal for probe" in result.stdout


@pytest.mark.parametrize(
    "terminal",
    ["aborted_refusal", "open_transaction_refusal", "offset_reset_hides_contradiction"],
)
def test_probe_never_passes_on_an_uncommitted_or_lost_record(
    tmp_path: Path, terminal: str
) -> None:
    """Codex 21:00Z: read_committed, and no silent offset reset."""
    result = _run_probe(
        tmp_path, group_present=True, members=1, join="demand", terminal=terminal
    )
    assert result.returncode == 1, _out(result)
    assert "typed refusal for probe" not in result.stdout


def test_probe_reads_past_an_aborted_batch_to_the_refusal(tmp_path: Path) -> None:
    result = _run_probe(
        tmp_path,
        group_present=True,
        members=1,
        join="demand",
        terminal="aborted_batch_then_refusal",
    )
    assert result.returncode == 0, _out(result)
    assert "terminal_failure_cause=provider_error" not in result.stdout


@pytest.mark.parametrize("terminal", ["post_snapshot_contradiction", "compaction_hole"])
def test_probe_judges_only_the_captured_window_by_offset(
    tmp_path: Path, terminal: str
) -> None:
    """Codex 21:00Z: arrivals after the captured end wait for the next poll;
    holes in the window do not shorten it."""
    result = _run_probe(
        tmp_path, group_present=True, members=1, join="demand", terminal=terminal
    )
    assert result.returncode == 0, _out(result)
    assert "typed refusal for probe" in result.stdout
