# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket contracts' own checks exit non-zero when their criterion is unmet (review of omnimarket#6306, finding 6).

Each check is run as the Done gate runs it, in a shell, with a stand-in ``docker`` or ``onex`` first on PATH,
once with the criterion met (exit 0) and once with it unmet (non-zero). A check that cannot fail proves nothing.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACTS = Path(__file__).resolve().parents[4] / "contracts"
SYSTEM_PATH = "/usr/bin:/bin"
HEAD = "0123456789abcdef0123456789abcdef01234567"


def _check(ticket: str, evidence_id: str) -> str:
    doc = yaml.safe_load((CONTRACTS / f"{ticket}.yaml").read_text())
    (entry,) = [e for e in doc["dod_evidence"] if e["id"] == evidence_id]
    (check,) = entry["checks"]
    return str(check["check_value"])


def _stand_in(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run(command: str, tmp_path: Path, *, extra: dict[str, str] | None = None) -> int:
    env = {"PATH": f"{tmp_path}:{SYSTEM_PATH}", "HOME": str(tmp_path), **(extra or {})}
    return subprocess.run(
        ["bash", "-c", command], env=env, capture_output=True, text=True, check=False
    ).returncode


def test_docker_log_check_fails_when_docker_is_absent_or_the_log_is_empty(
    tmp_path: Path,
) -> None:
    command = _check("OMN-24634", "dod-omn24634-dev-runtime-read")
    assert (
        _run(command, tmp_path) != 0
    )  # no docker on PATH: the unmet case the old check passed
    _stand_in(tmp_path, "docker", "true")
    assert (
        _run(command, tmp_path) != 0
    )  # a container that logs nothing is not a reading of zero
    _stand_in(tmp_path, "docker", "echo started")
    assert _run(command, tmp_path) == 0  # positive control
    _stand_in(tmp_path, "docker", 'echo "RuntimeError: no running event loop"')
    assert _run(command, tmp_path) != 0


def test_row_count_check_fails_without_an_install_time_or_a_database(
    tmp_path: Path,
) -> None:
    command = _check("OMN-24634", "dod-omn24634-dev-runtime-rows")
    assert _run(command, tmp_path) != 0
    assert (
        _run(command, tmp_path, extra={"ONEX_FIX_INSTALLED_AT": "2026-11-16T00:00:00Z"})
        != 0
    )
    both = {
        "ONEX_FIX_INSTALLED_AT": "2026-11-16T00:00:00Z",
        "OMNINODE_INTERNAL_DB_URL": "postgresql://stand-in/db",
    }
    _stand_in(tmp_path, "psql", "echo f")  # a window with a NULL row, or none at all
    assert _run(command, tmp_path, extra=both) != 0
    _stand_in(tmp_path, "psql", "echo t")  # positive control
    assert _run(command, tmp_path, extra=both) == 0


def _onex_answering(tmp_path: Path, comments: list[str]) -> None:
    answer = {
        "verb": "pr-view",
        "result": {
            "head_sha": HEAD,
            "comments": [{"body": body} for body in comments],
        },
    }
    (tmp_path / "answer.json").write_text(json.dumps(answer))
    _stand_in(tmp_path, "onex", f'cat "{tmp_path}/answer.json"')


def _answer(numbers: range, verdict: str = "fixed") -> str:
    lines = "\n".join(f"{n}. {verdict}: evidence" for n in numbers)
    return f"Head {HEAD[:12]}\n{lines}"


def test_findings_comment_check_fails_until_a_comment_answers_every_finding(
    tmp_path: Path,
) -> None:
    command = _check("OMN-24644", "dod-omn24644-findings-comment")
    _onex_answering(tmp_path, [])
    assert _run(command, tmp_path) != 0  # no comment at all: always exit 0 before
    _onex_answering(tmp_path, [_answer(range(1, 6))])
    assert _run(command, tmp_path) != 0  # finding 6 unanswered
    _onex_answering(
        tmp_path, ["1. fixed\n2. fixed\n3. fixed\n4. fixed\n5. fixed\n6. fixed"]
    )
    assert _run(command, tmp_path) != 0  # answers that do not name the head
    _onex_answering(tmp_path, [_answer(range(1, 7), "argued")])
    assert _run(command, tmp_path) == 0  # positive control
    _onex_answering(tmp_path, [_answer(range(1, 7))])
    assert _run(command, tmp_path) == 0
    _stand_in(tmp_path, "onex", "exit 1")
    assert _run(command, tmp_path) != 0  # the read itself failing is a failure
