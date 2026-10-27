# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Small fixed, offline lane fixtures. Checkers are never given to the subject.

Git objects below are fixture data, written with hash-object, not product
commits. Linked fixture worktrees share the caller repository and its hooks.
No repository config, hook or external review thread is modified.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

TASK_TYPES = (
    "rebase-conflict",
    "failing-test-fix",
    "ci-log-diagnosis",
    "ticket-evidence-fix",
    "review-thread-reply",
    "docs-edit",
    "host-read-only",
)


def fixed_tasks() -> list[dict[str, Any]]:
    return [
        {"item_key": f"{kind}-{i}", "task_class": kind, "variant": i}
        for kind in TASK_TYPES
        for i in range(1, 4)
    ]


def files_for(task: dict[str, Any]) -> tuple[dict[str, str], str]:
    if task.get("cohort") == "recent":
        return task["files"], task["prompt"]
    kind, i = task["task_class"], task["variant"]
    cap = i * 10
    if kind == "rebase-conflict":
        return (
            {
                "limits.py": "def clamp(n):\n    return max(n, 0)\n",
                "README.md": f"clamp must return 0 for negatives, n inside [0,{cap}], and {cap} above it.\n",
            },
            "Resolve the in-progress rebase conflict in limits.py. Preserve both the lower bound from the topic and the upper bound from upstream, then finish the rebase. Do not drop the topic commit.",
        )
    if kind == "failing-test-fix":
        sources = [
            (
                "def enabled(row):\n    return 'endpoint_url' in row\n",
                "def enabled(row):\n    return bool(row.get('endpoint_url'))\n",
                "assert enabled({'endpoint_url': 'https://example.invalid'})\nassert not enabled({'endpoint_url': None})\nassert not enabled({})\n",
            ),
            (
                "def next_engine(engine):\n    return engine\n",
                "def next_engine(engine):\n    return {'code-edit': 'glm', 'glm': 'codex'}.get(engine)\n",
                "assert next_engine('code-edit') == 'glm'\nassert next_engine('glm') == 'codex'\nassert next_engine('sonnet') is None\n",
            ),
            (
                "def active(head, base):\n    return bool(head)\n",
                "def active(head, base):\n    return bool(head) and head == base\n",
                "assert active('abc', 'abc')\nassert not active('abc', 'def')\nassert not active('', '')\n",
            ),
        ]
        broken, _, assertions = sources[i - 1]
        return (
            {
                "subject.py": broken,
                "test_subject.py": "from subject import *\n" + assertions,
            },
            "Fix subject.py so python3 test_subject.py passes. Preserve the tests unchanged.",
        )
    if kind == "ci-log-diagnosis":
        names = [
            "test_disabled_added_backend_is_excluded",
            "test_execution_adapter_preserves_result",
            "test_docs_only_floor_matches_runtime",
        ]
        name = names[i - 1]
        return (
            {
                "ci.log": f"collecting ... collected 203 items\nPASSED tests/test_control.py::test_control\nFAILED tests/test_lane.py::{name} - AssertionError: expected refusal\n1 failed, 202 passed\nwarning: optional telemetry unavailable\n"
            },
            "Read ci.log. Write answer.json containing exactly failing_test (the complete pytest node id) and failure_kind='assertion'. Name the failing test, not the telemetry warning.",
        )
    if kind == "ticket-evidence-fix":
        return (
            {
                "contract.json": json.dumps(
                    {
                        "ticket_id": "FIXTURE-1",
                        "acceptance": [f"AC{i}"],
                        "dod_evidence": [],
                    }
                ),
                "test_subject.py": "assert 2 + 2 == 4\n",
            },
            f"Repair contract.json by adding one dod_evidence entry with binds_ac=['AC{i}'], and checks=[{{'check_type':'test_passes','check_value':'python3 test_subject.py','cwd':'.'}}]. Preserve the acceptance criterion and ticket id.",
        )
    if kind == "review-thread-reply":
        return (
            {
                "thread.json": json.dumps(
                    {
                        "thread_id": f"thread-{i}",
                        "path": "subject.py",
                        "finding": "disabled backend admitted",
                        "fixed_head": f"fixture-head-{i}",
                        "test": "test_disabled_backend_refused",
                        "observed": "1 passed",
                    }
                )
            },
            "Draft the reply to thread.json in answer.json. Use exactly thread_id, head, test, observed, reply. Copy the first four facts (head is fixed_head) exactly. reply must say 'Disabled backends are excluded.' Do not claim a merge, deployment, or actual posting.",
        )
    if kind == "docs-edit":
        return (
            {
                "brief.md": f"# Lane brief\n\nRequired host: h20{i + 1}\nFinal check: python3 test_subject.py\nNever weaken a hook.\n"
            },
            "Edit brief.md so Required host is h201. Keep every other line byte-for-byte unchanged.",
        )
    return (
        {
            "host-request.json": json.dumps(
                {
                    "fields": ["hostname", "kernel", "python_major"],
                    "mutations_allowed": False,
                }
            )
        },
        "Read this host using Python os.uname() and sys.version_info.major. Write answer.json with exactly hostname (uname.nodename), kernel (uname.release), python_major (integer). Do not change any other file or host state.",
    )


def oracle(task: dict[str, Any], root: Path) -> None:
    """Positive control used only by checker tests, never exposed to a harness."""
    if task.get("cohort") == "recent":
        from tests.harness_evals.recent import oracle_recent

        oracle_recent(task, root)
        return
    kind, i = task["task_class"], task["variant"]
    if kind == "rebase-conflict":
        (root / "limits.py").write_text(
            f"def clamp(n):\n    return min(max(n, 0), {i * 10})\n"
        )
    elif kind == "failing-test-fix":
        answers = [
            "def enabled(row):\n    return bool(row.get('endpoint_url'))\n",
            "def next_engine(engine):\n    return {'code-edit': 'glm', 'glm': 'codex'}.get(engine)\n",
            "def active(head, base):\n    return bool(head) and head == base\n",
        ]
        (root / "subject.py").write_text(answers[i - 1])
    elif kind == "ticket-evidence-fix":
        contract = json.loads((root / "contract.json").read_text())
        contract["dod_evidence"] = [
            {
                "binds_ac": [f"AC{i}"],
                "checks": [
                    {
                        "check_type": "test_passes",
                        "check_value": "python3 test_subject.py",
                        "cwd": ".",
                    }
                ],
            }
        ]
        (root / "contract.json").write_text(json.dumps(contract))
    elif kind == "docs-edit":
        (root / "brief.md").write_text(
            files_for(task)[0]["brief.md"].replace(f"h20{i + 1}", "h201")
        )
    else:
        if kind == "ci-log-diagnosis":
            names = [
                "test_disabled_added_backend_is_excluded",
                "test_execution_adapter_preserves_result",
                "test_docs_only_floor_matches_runtime",
            ]
            answer = {
                "failing_test": f"tests/test_lane.py::{names[i - 1]}",
                "failure_kind": "assertion",
            }
        elif kind == "review-thread-reply":
            answer = {
                "thread_id": f"thread-{i}",
                "head": f"fixture-head-{i}",
                "test": "test_disabled_backend_refused",
                "observed": "1 passed",
                "reply": "Disabled backends are excluded.",
            }
        else:
            answer = {
                "hostname": os.uname().nodename,
                "kernel": os.uname().release,
                "python_major": sys.version_info.major,
            }
        (root / "answer.json").write_text(json.dumps(answer))


def _exec(root: Path, source: str) -> bool:
    # Check code in a fresh interpreter; never exec subject output in the runner.
    try:
        return (
            subprocess.run(
                [sys.executable, "-I", "-c", source],
                cwd=root,
                capture_output=True,
                timeout=10,
                check=False,
            ).returncode
            == 0
        )
    except subprocess.TimeoutExpired:
        return False


def check(task: dict[str, Any], root: Path, *, git_dir: str | None = None) -> bool:
    """Authoritative checker. All invariant inputs are taken from fixed_tasks."""
    kind, i = task["task_class"], task["variant"]
    original, _ = files_for(task)
    try:
        allowed = {
            "rebase-conflict": {"limits.py"},
            "failing-test-fix": {"subject.py"},
            "ticket-evidence-fix": {"contract.json"},
            "docs-edit": {"brief.md"},
        }.get(kind, {"answer.json"})
        for name, content in original.items():
            if name not in allowed and (root / name).read_text() != content:
                return False
        if kind == "rebase-conflict":
            if any(
                mark in (root / "limits.py").read_text()
                for mark in ("<<<<<<<", "=======", ">>>>>>>")
            ):
                return False
            if git_dir:
                if (Path(git_dir) / "rebase-merge").exists():
                    return False
                expected = json.loads((root.parent / "checker-state.json").read_text())
                result = subprocess.run(
                    [
                        "git",
                        "--git-dir",
                        git_dir,
                        "--work-tree",
                        str(root),
                        "rev-list",
                        "--parents",
                        "-n",
                        "1",
                        "HEAD",
                    ],
                    env=scrub_git_location_env(os.environ),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                parents = result.stdout.split()
                if (
                    result.returncode
                    or len(parents) != 2
                    or parents[1] != expected["onto"]
                ):
                    return False
            return _exec(
                root,
                f"import runpy; f=runpy.run_path('limits.py')['clamp']; assert [f(n) for n in (-100,0,1,{i * 10},{i * 10 + 1},1000)] == [0,0,1,{i * 10},{i * 10},{i * 10}]",
            )
        if kind == "failing-test-fix":
            return _exec(
                root,
                "import runpy,sys; sys.path.insert(0,'.'); runpy.run_path('test_subject.py')",
            )
        if kind == "ticket-evidence-fix":
            doc = json.loads((root / "contract.json").read_text())
            return (
                doc.get("ticket_id") == "FIXTURE-1"
                and doc.get("acceptance") == [f"AC{i}"]
                and len(doc.get("dod_evidence", [])) == 1
                and doc["dod_evidence"][0].get("binds_ac") == [f"AC{i}"]
                and doc["dod_evidence"][0].get("checks")
                == [
                    {
                        "check_type": "test_passes",
                        "check_value": "python3 test_subject.py",
                        "cwd": ".",
                    }
                ]
                and _exec(root, "import runpy; runpy.run_path('test_subject.py')")
            )
        if kind == "docs-edit":
            return (root / "brief.md").read_text() == original["brief.md"].replace(
                f"h20{i + 1}", "h201"
            )
        if kind == "host-read-only" and {p.name for p in root.iterdir()} - {".git"} != {
            "host-request.json",
            "answer.json",
        }:
            return False
        answer = json.loads((root / "answer.json").read_text())
        if kind == "ci-log-diagnosis":
            line = next(
                line
                for line in original["ci.log"].splitlines()
                if line.startswith("FAILED ")
            )
            return answer == {
                "failing_test": line.split()[1],
                "failure_kind": "assertion",
            }
        if kind == "review-thread-reply":
            return answer == {
                "thread_id": f"thread-{i}",
                "head": f"fixture-head-{i}",
                "test": "test_disabled_backend_refused",
                "observed": "1 passed",
                "reply": "Disabled backends are excluded.",
            }
        return answer == {
            "hostname": os.uname().nodename,
            "kernel": os.uname().release,
            "python_major": sys.version_info.major,
        }
    except (OSError, ValueError, TypeError, KeyError, StopIteration):
        return False


def _git(
    repo: Path, *args: str, data: bytes | None = None, ok: bool = True
) -> subprocess.CompletedProcess[bytes]:
    command = ["git", "-C", str(repo)]
    git_dir = os.environ.get("GIT_DIR")
    if git_dir:
        command += ["--git-dir", git_dir, "--work-tree", str(repo)]
    return subprocess.run(
        [*command, *args],
        env=scrub_git_location_env(os.environ),
        input=data,
        capture_output=True,
        check=ok,
        timeout=30,
    )


def _object(repo: Path, kind: str, content: bytes) -> str:
    return (
        _git(repo, "hash-object", "-t", kind, "-w", "--stdin", data=content)
        .stdout.decode()
        .strip()
    )


def _commit(repo: Path, files: dict[str, str], parent: str | None, message: str) -> str:
    entries = b"".join(
        b"100644 "
        + name.encode()
        + b"\0"
        + bytes.fromhex(_object(repo, "blob", body.encode()))
        for name, body in sorted(files.items())
    )
    tree = _object(repo, "tree", entries)
    headers = f"tree {tree}\n" + (f"parent {parent}\n" if parent else "")
    return _object(
        repo,
        "commit",
        (
            headers
            + "author Harness Fixture <fixture@example.invalid> 1700000000 +0000\ncommitter Harness Fixture <fixture@example.invalid> 1700000000 +0000\n\n"
            + message
            + "\n"
        ).encode(),
    )


def materialize(task: dict[str, Any], root: Path, repo: Path) -> tuple[str, str]:
    """Return prompt and fixture git dir; parent GIT_DIR/WORK_TREE stay intact."""
    files, prompt = files_for(task)
    if task["task_class"] == "rebase-conflict":
        base_files = {**files, "limits.py": "def clamp(n):\n    return n\n"}
        base = _commit(repo, base_files, None, "fixture base")
        onto = _commit(
            repo,
            {
                **files,
                "limits.py": f"def clamp(n):\n    return min(n, {task['variant'] * 10})\n",
            },
            base,
            "fixture upstream upper bound",
        )
        head = _commit(repo, files, base, "fixture topic lower bound")
    else:
        head = _commit(repo, files, None, "fixture " + task["item_key"])
        onto = base = ""
    _git(repo, "worktree", "add", "--detach", str(root), head)
    git_dir = Path((root / ".git").read_text().strip().removeprefix("gitdir: "))
    if onto:
        result = subprocess.run(
            [
                "git",
                "--git-dir",
                str(git_dir),
                "--work-tree",
                str(root),
                "rebase",
                "--onto",
                onto,
                base,
            ],
            env=scrub_git_location_env(os.environ),
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode == 0 or not (git_dir / "rebase-merge").exists():
            raise RuntimeError("fixture did not produce a conflicted rebase")
    (root.parent / "checker-state.json").write_text(
        json.dumps({"head": head, "onto": onto, "base": base})
    )
    return prompt, str(git_dir)


def fixture_digest() -> str:
    body = json.dumps(
        {
            "items": [(t, files_for(t)) for t in fixed_tasks()],
            "checker": inspect.getsource(check),
        },
        sort_keys=True,
    )
    return hashlib.sha256(body.encode()).hexdigest()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Trusted fixture checker; no model calls"
    )
    parser.add_argument("--item-key", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--git-dir")
    args = parser.parse_args()
    if __package__:
        from .recent import check_task, recent_tasks
    else:
        from recent import check_task, recent_tasks

    selected = next(
        (
            task
            for task in fixed_tasks() + recent_tasks()
            if task["item_key"] == args.item_key
        ),
        None,
    )
    if selected is None:
        parser.error("unknown fixed fixture item")
    raise SystemExit(0 if check_task(selected, args.root, git_dir=args.git_dir) else 1)
