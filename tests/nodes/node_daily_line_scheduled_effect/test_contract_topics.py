# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The terminal event node_daily_line_scheduled_effect declares is a topic it publishes (OMN-25163)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE_DIR = REPO / "src/omnimarket/nodes/node_daily_line_scheduled_effect"
CONTRACT = NODE_DIR / "contract.yaml"
METADATA = NODE_DIR / "metadata.yaml"
RUNNER_CONTRACT = REPO / "src/omnimarket/nodes/node_daily_line_effect/contract.yaml"
OWNER_CONTRACT = REPO / "contracts/OMN-25163.yaml"
REVIEW_CONTRACT = REPO / "contracts/OMN-25307.yaml"
READ_ENTRY = "dod-omn25163-ac3-schedule-run-read"
LAB_ENTRY = "dod-omn25307-ac5"


def test_terminal_event_is_the_declared_output_and_is_published() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert loaded["terminal_event"] == "onex.evt.omnimarket.schedule-run.v1"
    assert (
        "onex.evt.omnimarket.schedule-run.v1" in loaded["event_bus"]["publish_topics"]
    )


def test_subscription_topic_and_operation_are_as_declared() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    subs = loaded["input_subscriptions"]
    assert len(subs) == 1
    assert subs[0]["topic"] == "onex.intent.platform.runtime-tick.v1"
    assert subs[0]["operation"] == "daily_line.scheduled_tick"


def test_timeout_ms_is_the_runners_own_five_minute_unit_limit() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    runner = yaml.safe_load(RUNNER_CONTRACT.read_text(encoding="utf-8"))
    # One run reads the ledger (60 s bound) and appends one row (60 s bound), plus the floor read and the file write; the
    # runner's own timer unit allows 300 s for the same run, so the scheduled node's limit is exactly that, not a floor.
    assert loaded["descriptor"]["timeout_ms"] == 300_000
    assert (
        loaded["descriptor"]["timeout_ms"]
        == runner["schedule"]["timeout_seconds"] * 1000
    )
    bounds = (
        runner["runtime"]["ledger_read_timeout_seconds"]
        + runner["runtime"]["ledger_append_timeout_seconds"]
    )
    assert loaded["descriptor"]["timeout_ms"] > bounds * 1000 * 2


def test_metadata_declares_the_real_capabilities() -> None:
    metadata = yaml.safe_load(METADATA.read_text(encoding="utf-8"))
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    caps = metadata["capabilities"]
    # The run reads the ledger over the bus, reads the floor record under the registry home and writes daily-line.json
    # and one STATUS row there: it is not network-free, repo-free or side-effect-free.
    assert caps["requires_network"] is True
    assert caps["requires_repo"] is True
    assert caps["side_effect_class"] == "write"
    assert contract["descriptor"]["purity"] == "effectful"
    assert caps["side_effect_class"] != "none"


def _entries(contract_path: Path) -> list[dict[str, Any]]:
    loaded = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return list(loaded["dod_evidence"])


def _entry(contract_path: Path, entry_id: str) -> dict[str, Any]:
    (found,) = [e for e in _entries(contract_path) if e["id"] == entry_id]
    return found


def _command(contract_path: Path, entry_id: str) -> str:
    (check,) = _entry(contract_path, entry_id)["checks"]
    return str(check["check_value"])


def _ac(contract_path: Path, ac_id: str) -> str:
    loaded = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return next(
        a for r in loaded["requirements"] for a in r["acceptance"] if a["id"] == ac_id
    )["statement"]


def test_contract_ac3_is_bound_at_the_hosted_gate_only_to_the_read_behaviour_test() -> (
    None
):
    entries = _entries(OWNER_CONTRACT)
    # The hosted gate refuses a local_done_gate item that carries binds_ac (scoped_item_binding.py), and it demotes a
    # criterion no item binds to unproven (handler_dod_verify.py), so AC3 needs one hosted binding. It is the test of the
    # read's own behaviour below: it can fail, and it does not call AC3 proven. A grep of contract.yaml, a gh api call or
    # a string match of this YAML is not that evidence.
    bound = [e for e in entries if "AC3" in e.get("binds_ac", [])]
    assert [e["id"] for e in bound] == ["dod-omn25163-ac3-read-behaviour"]
    assert "execution_scope" not in bound[0]
    (check,) = bound[0]["checks"]
    assert check["check_type"] == "test_passes"
    assert "test_contract_topics.py" in check["check_value"]
    assert "-k 'ac3_read'" in check["check_value"]
    assert "not proven at the hosted gate" in bound[0]["description"]
    for entry in entries:
        for entry_check in entry["checks"]:
            if "AC3" in entry.get("binds_ac", []):
                assert "contract.yaml" not in entry_check["check_value"].replace(
                    "test_contract_topics.py", ""
                )
                assert "gh api" not in entry_check["check_value"]


def test_contract_ac3_dev_read_is_the_done_gate_entry_and_binds_nothing() -> None:
    read = _entry(OWNER_CONTRACT, "dod-omn25163-ac3-schedule-run-read")
    assert read["execution_scope"] == "local_done_gate"
    assert "binds_ac" not in read
    value = _command(OWNER_CONTRACT, "dod-omn25163-ac3-schedule-run-read")
    assert "kcat" in value
    assert "onex.evt.omnimarket.schedule-run.v1" in value
    assert "daily_line" in value
    assert "contract.yaml" not in value
    assert "gh api" not in value


def test_contract_ac3_selector_does_not_match_the_module_name() -> None:
    # pytest -k matches the module name too: 'contract' or 'metadata' in an expression selects the whole of this file.
    module = Path(__file__).stem
    for contract_path in (OWNER_CONTRACT, REVIEW_CONTRACT):
        for entry in _entries(contract_path):
            if "AC3" not in entry.get("binds_ac", []):
                continue
            for check in entry["checks"]:
                match = re.search(r"-k '([^']*)'", check["check_value"])
                assert match, check["check_value"]
                terms = re.split(r"\s+(?:or|and)\s+|\s+", match.group(1))
                for term in terms:
                    assert term in ("or", "and", "not") or term not in module, term


def test_control_a_selector_term_inside_the_module_name_is_found() -> None:
    assert "contract" in Path(__file__).stem


def _event_json(
    *,
    job_id: str = "daily_line",
    outcome: str = "completed",
    fired_at: datetime = datetime(2026, 11, 17, 12, 30, tzinfo=UTC),
) -> str:
    return ModelScheduleRunEvent(
        job_id=job_id,
        node_name="node_daily_line_scheduled_effect",
        tick_id="tick-1",
        scheduled_for=fired_at,
        fired_at=fired_at,
        outcome=outcome,  # type: ignore[arg-type]
        detail="Daily line published.",
    ).model_dump_json()


_FAKE_KCAT = """#!/usr/bin/env python3
import os
import signal
import sys

signal.signal(signal.SIGPIPE, signal.SIG_DFL)
with open(os.environ["KCAT_ARGS"], "w", encoding="utf-8") as handle:
    handle.write("\\n".join(sys.argv[1:]))
data = open(os.environ["KCAT_EVENTS"], encoding="utf-8").read()
for _ in range(int(os.environ.get("KCAT_REPEAT", "1"))):
    sys.stdout.write(data)
    sys.stdout.flush()
"""


def _run_read(
    tmp_path: Path,
    events: list[str],
    *,
    command: str | None = None,
    repeat: int = 1,
    env_extra: dict[str, str | None] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    kcat = bin_dir / "kcat"
    kcat.write_text(_FAKE_KCAT, encoding="utf-8")
    kcat.chmod(0o755)
    (tmp_path / "events.jsonl").write_text(
        "".join(f"{e}\n" for e in events), encoding="utf-8"
    )
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "KCAT_ARGS": str(tmp_path / "args.txt"),
        "KCAT_EVENTS": str(tmp_path / "events.jsonl"),
        "KCAT_REPEAT": str(repeat),
        "KAFKA_BOOTSTRAP_SERVERS": "broker.invalid:9092",
        "DEPLOYED_AT": "2026-11-17T12:00:00Z",
    }
    for key, value in (env_extra or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    result = subprocess.run(
        ["bash", "-c", command or _command(OWNER_CONTRACT, READ_ENTRY)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    args_file = tmp_path / "args.txt"
    args = (
        args_file.read_text(encoding="utf-8").split("\n") if args_file.exists() else []
    )
    return result, args


def test_ac3_read_passes_on_a_completed_daily_line_event_after_the_deploy(
    tmp_path: Path,
) -> None:
    result, _ = _run_read(tmp_path, [_event_json()])
    assert result.returncode == 0, result.stderr


def test_ac3_read_passes_on_a_match_even_when_the_topic_is_large(
    tmp_path: Path,
) -> None:
    # grep -q exits on its first match, kcat takes SIGPIPE, pipefail then fails the check: the read drains its input.
    result, _ = _run_read(tmp_path, [_event_json()] * 50, repeat=2000)
    assert result.returncode == 0, result.stderr


def test_control_ac3_read_with_grep_q_fails_on_a_match_of_a_large_topic(
    tmp_path: Path,
) -> None:
    old = (
        'bash -o pipefail -c \'kcat -C -b "${KAFKA_BOOTSTRAP_SERVERS:?x}" '
        '-t onex.evt.omnimarket.schedule-run.v1 -o -500 -e -q | grep -Eq "\\"job_id\\": ?\\"daily_line\\""\''
    )
    spaced = _event_json().replace('"job_id":"', '"job_id": "')
    result, _ = _run_read(tmp_path, [spaced] * 50, command=old, repeat=2000)
    assert result.returncode != 0


def test_ac3_read_fails_on_a_failed_event(tmp_path: Path) -> None:
    result, _ = _run_read(tmp_path, [_event_json(outcome="failed")])
    assert result.returncode != 0


def test_ac3_read_fails_on_an_event_fired_before_the_deploy(tmp_path: Path) -> None:
    before = datetime(2026, 11, 17, 11, 59, tzinfo=UTC)
    result, _ = _run_read(tmp_path, [_event_json(fired_at=before)])
    assert result.returncode != 0


def test_ac3_read_fails_on_another_jobs_event_and_on_an_empty_topic(
    tmp_path: Path,
) -> None:
    other, _ = _run_read(tmp_path, [_event_json(job_id="floor_fill")])
    assert other.returncode != 0
    empty, _ = _run_read(tmp_path, [])
    assert empty.returncode != 0


def test_ac3_read_passes_when_a_failed_event_is_followed_by_a_completed_one(
    tmp_path: Path,
) -> None:
    result, _ = _run_read(
        tmp_path,
        [_event_json(outcome="failed"), _event_json(), _event_json(job_id="x")],
    )
    assert result.returncode == 0, result.stderr


def test_ac3_read_starts_from_the_deploy_time_offset(tmp_path: Path) -> None:
    _, args = _run_read(tmp_path, [_event_json()])
    expected = int(datetime(2026, 11, 17, 12, 0, tzinfo=UTC).timestamp() * 1000)
    assert args[args.index("-o") + 1] == f"s@{expected}"
    assert "-500" not in args


def test_ac3_read_refuses_to_run_without_the_deploy_time(tmp_path: Path) -> None:
    result, _ = _run_read(tmp_path, [_event_json()], env_extra={"DEPLOYED_AT": None})
    assert result.returncode != 0
    assert "DEPLOYED_AT" in result.stderr


def test_ac3_read_has_no_grep_quiet_flag() -> None:
    value = _command(OWNER_CONTRACT, READ_ENTRY)
    assert "grep" in value
    assert not re.search(r"grep\s+(?:-\w*\s+)*-\w*q", value)
    assert "> /dev/null" in value


def test_contract_ac2_falsifier_and_check_run_the_entry_point_seam_too() -> None:
    seam = "tests/nodes/node_daily_line_scheduled_effect/test_registration_seam.py"
    acl = "tests/nodes/node_schedule_run_reducer/test_runtime_role_acl_seam.py"
    statement = _ac(OWNER_CONTRACT, "AC2")
    assert seam in statement
    assert acl in statement
    value = _command(OWNER_CONTRACT, "dod-omn25163-ac2")
    assert seam in value
    assert acl in value
    loaded = yaml.safe_load(OWNER_CONTRACT.read_text(encoding="utf-8"))
    # The directory run of the evidence requirement holds the seam file as well.
    assert seam.rsplit("/", 1)[0] in loaded["evidence_requirements"][0]["command"]


def _fake_gh(tmp_path: Path, comments: list[str]) -> dict[str, str]:
    bin_dir = tmp_path / "ghbin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        'open(os.environ["GH_ARGS"], "w").write(" ".join(sys.argv[1:]))\n'
        'print("\\n".join(json.loads(os.environ["GH_COMMENTS"])))\n',
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_ARGS": str(tmp_path / "gh_args.txt"),
        "GH_COMMENTS": json.dumps(comments),
    }


def _run_lab_check(tmp_path: Path, comments: list[str], head: str) -> int:
    tmp_path = Path(tempfile.mkdtemp(dir=tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run(
        [
            *git,
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
    )
    real_head = subprocess.run(
        [*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    env = {
        **os.environ,
        **_fake_gh(
            tmp_path,
            [
                c.replace("<head>", real_head if head == "real" else head)
                for c in comments
            ],
        ),
    }
    return subprocess.run(
        ["bash", "-c", _command(REVIEW_CONTRACT, LAB_ENTRY)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    ).returncode


def test_contract_review_ac5_passes_for_a_lab_comment_naming_the_head(
    tmp_path: Path,
) -> None:
    assert _run_lab_check(tmp_path, ["Lab: head=<head>\nruns follow"], "real") == 0


def test_contract_review_ac5_fails_for_a_pr_with_a_head_and_no_lab_comment(
    tmp_path: Path,
) -> None:
    assert _run_lab_check(tmp_path, ["looks fine", "review comment"], "real") != 0
    assert _run_lab_check(tmp_path, [], "real") != 0


def test_contract_review_ac5_fails_for_a_lab_comment_naming_another_head(
    tmp_path: Path,
) -> None:
    other = "0" * 40
    assert _run_lab_check(tmp_path, [f"Lab: head={other}\nruns"], "real") != 0
    # A prefix of the head is not the head.
    assert _run_lab_check(tmp_path, ["Lab: head=<head>0\nruns"], "real") != 0


def test_contract_review_ac5_check_reads_the_comment_and_the_local_head() -> None:
    value = _command(REVIEW_CONTRACT, LAB_ENTRY)
    assert "git rev-parse HEAD" in value
    assert "Lab: head=" in value
    assert "comments" in value
    assert "head_sha" not in value
    entry = _entry(REVIEW_CONTRACT, LAB_ENTRY)
    assert entry["execution_scope"] == "local_done_gate"
    assert "binds_ac" not in entry


def test_contract_no_entry_hardcodes_the_lane_of_an_earlier_run() -> None:
    contract = yaml.safe_load(OWNER_CONTRACT.read_text(encoding="utf-8"))
    for entry in contract["dod_evidence"]:
        for check in entry["checks"]:
            assert "run5340" not in check["check_value"]


def test_contract_names_the_overlay_pull_request_order_in_the_ac3_text() -> None:
    contract = yaml.safe_load(OWNER_CONTRACT.read_text(encoding="utf-8"))
    ac3 = next(
        a for r in contract["requirements"] for a in r["acceptance"] if a["id"] == "AC3"
    )["statement"]
    assert "onex.evt.omnimarket.schedule-run.v1" in ac3
    assert "daily_line" in ac3
    assert "overlays" in ac3
    assert "after this pull request merges" in ac3


def test_timeout_worst_case_run_is_below_the_consumers_dispatch_deadline() -> None:
    # The runtime abandons (does not cancel) a tick dispatch at 0.85 of max_poll_interval_ms: 255 s at the 300 s library
    # default (omnibase_infra event_bus_kafka, OMN-19355). The runner bounds its own ledger read and append, so one run
    # ends well inside that, while descriptor.timeout_ms stays the node timer unit's 300 s.
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    runner = yaml.safe_load(RUNNER_CONTRACT.read_text(encoding="utf-8"))
    consumer_deadline_seconds = 300 * 0.85
    bounded = (
        runner["runtime"]["ledger_read_timeout_seconds"]
        + runner["runtime"]["ledger_append_timeout_seconds"]
    )
    assert loaded["descriptor"]["timeout_ms"] == 300_000
    assert bounded * 2 < consumer_deadline_seconds
