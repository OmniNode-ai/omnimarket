# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures for plans-today: a plans repository with a bare origin, ledger rows, a Linear snapshot and
its sidecar, the fake team identity declaration, and a fake omni plugin root. Every value is fake."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_plans_today_effect.handlers import (
    handler_plans_today as plans_today,
)
from omnimarket.nodes.node_plans_today_effect.runtime_plans_today import (
    main as cli_main,
)

REPO = Path(__file__).resolve().parents[3]
IDENTITY_FIXTURE = REPO / "tests" / "fixtures" / "team_identity" / "valid.yaml"
NOW = "2026-09-29T16:00:00Z"

# The fake declaration's entries (tests/fixtures/team_identity/valid.yaml).
OPERATOR_LINEAR = "00000000-0000-4000-8000-00000000f001"
MEMBER_A_LINEAR = "00000000-0000-4000-8000-00000000f002"
MEMBER_B_LINEAR = "00000000-0000-4000-8000-00000000f003"
MEMBER_A_GH = "fixture-member-a-gh"
MEMBER_A_PRINCIPAL = "fixture-member-a-dev"
MEMBER_B_PRINCIPAL = "fixture-member-b-dev"
UNKNOWN_LINEAR = "00000000-0000-4000-8000-0000000fffff"


def git(*args: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *map(str, args)],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )


def body(title: str, tickets: list[tuple[str, str]], extra: str = "") -> str:
    rows = "\n".join(
        f"| T{i} {name} | {ticket} |" for i, (name, ticket) in enumerate(tickets, 1)
    )
    return f"# {title}\n\n## Tasks\n\n| Task | Ticket |\n| -- | -- |\n{rows}\n{extra}\n"


def write_plans(clone: Path, plans: dict[str, str]) -> None:
    directory = clone / "beta" / "plans"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "PLAN_LIFECYCLE.yaml").write_text("", encoding="utf-8")
    for filename, text in plans.items():
        (directory / filename).write_text(text, encoding="utf-8")


def kb_fixture(
    root: Path, plans: dict[str, str], *, origin: bool = False
) -> tuple[Path | None, Path]:
    """The plans clone, its ``origin/main`` at the fixture commit. ``origin`` adds a bare remote to fetch."""
    clone = root / "kb"
    git("init", "-q", "-b", "main", clone)
    git("-C", clone, "config", "user.name", "Test")
    git("-C", clone, "config", "user.email", "test@example.invalid")
    write_plans(clone, plans)
    git("-C", clone, "add", "beta/plans")
    git("-C", clone, "commit", "-q", "-m", "fixture plans")
    if not origin:
        git("-C", clone, "update-ref", "refs/remotes/origin/main", "HEAD")
        return None, clone
    bare = root / "origin.git"
    git("clone", "-q", "--bare", clone, bare)
    git("-C", clone, "remote", "add", "origin", bare)
    git("-C", clone, "fetch", "-q", "origin", "main")
    return bare, clone


def pr(
    ticket: str,
    cls: str = "red",
    state: str = "OPEN",
    updated: str = "2026-09-29T15:00:00Z",
) -> dict[str, Any]:
    """A pr-watcher state entry."""
    return {
        "cls": cls,
        "facts": {
            "state": state,
            "title": f"fix({ticket}): x",
            "body": "",
            "head_ref": "b",
            "updated_at": updated,
        },
        "ci": {"red": ["c"] if cls == "red" else [], "pending": []},
    }


def row(
    number: int,
    ts: str,
    kind: str,
    lane: str,
    ticket: str,
    scope: str = "scope text",
    principal: str | None = None,
    pr_cell: str | None = None,
) -> str:
    cells = [f"L{number} {ts}", kind, f"lane={lane}", f"ticket={ticket}", "actor=x"]
    if principal:
        cells.append(f"principal={principal}")
    if pr_cell:
        cells.append(f"pr={pr_cell}")
    return " | ".join([*cells, scope])


def ledger(rows: list[str]) -> str:
    newest = max((line.split(" ")[1] for line in rows), default="none")
    return (
        f"ROWS n={len(rows)} newest={newest} kinds=CLAIM:1 detail=x\n"
        + "\n".join(rows)
        + "\n"
    )


def issue(
    ticket: str,
    assignee: str = MEMBER_A_LINEAR,
    status: str = "Backlog",
    prs: list[str] | None = None,
) -> dict[str, Any]:
    """One sidecar issue; ``prs`` None records that the attachments could not be read."""
    return {
        "id": ticket,
        "title": f"t {ticket}",
        "status": status,
        "labels": [],
        "project": "p",
        "assignee": assignee,
        "pull_requests": prs,
    }


def make_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plans: dict[str, str],
    prs: dict[str, Any] | None = None,
    rows: list[str] | None = None,
    issues: list[dict[str, Any]] | None = None,
    *,
    subject: str | None = OPERATOR_LINEAR,
    sidecar_issues: list[dict[str, Any]] | None = None,
    watcher: bool = True,
    origin: bool = False,
) -> dict[str, Any]:
    """The plans, the ledger rows, the snapshot (``linear.txt``) and its sidecar (``linear.json``).

    ``subject`` is who the sidecar was read as (the operator by default); ``None`` writes no sidecar.
    """
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    roots = tmp_path / "overlays"
    (roots / "team_identity").mkdir(parents=True)
    shutil.copyfile(IDENTITY_FIXTURE, roots / "team_identity" / "overlay.yaml")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(roots))
    bare, clone = kb_fixture(tmp_path, plans, origin=origin)
    monkeypatch.setenv("ONEX_PLANS_KB_PATH", str(clone))
    watcher_file = tmp_path / "watcher.json"
    if watcher:
        watcher_file.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "last_tick": "2026-09-29T15:55:00Z",
                    "prs": prs or {},
                    "repos": {},
                    "heads": {},
                }
            )
        )
    ledger_file = tmp_path / "ledger.txt"
    ledger_file.write_text(ledger(rows or []))
    linear = tmp_path / "linear.txt"
    linear.write_text(json.dumps({"issues": issues or []}))
    sidecar = tmp_path / "linear.json"
    if subject is not None:
        sidecar.write_text(
            json.dumps({"subject": subject, "issues": sidecar_issues or []})
        )
    return {
        "origin": bare,
        "kb": clone,
        "watcher": watcher_file,
        "ledger": ledger_file,
        "linear": linear,
        "sidecar": sidecar,
        "roots": roots,
        "out": tmp_path / "out",
    }


def invoke(
    fx: dict[str, Any],
    *,
    now: str = NOW,
    no_msg: bool = True,
    linear: bool | str = True,
    out: Path | None = None,
    watcher: bool = True,
    extra: list[str] | None = None,
) -> tuple[int, dict[str, Any] | None]:
    out = out or fx["out"]
    argv = [
        "--no-fetch",
        "--summaries",
        "none",
        "--ledger-rows-file",
        str(fx["ledger"]),
        "--kb",
        str(fx["kb"]),
        "--out-dir",
        str(out),
        "--now",
        now,
    ]
    if watcher:
        argv += ["--watcher", str(fx["watcher"])]
    if no_msg:
        argv.append("--no-msg")
    if linear is True:
        argv += ["--linear-snapshot", str(fx["linear"])]
    elif linear == "disabled":
        argv.append("--no-linear")
    argv += extra or []
    code = cli_main(argv)
    path = out / "plans-today.json"
    doc = json.loads(path.read_text()) if path.exists() else None
    return code, doc


def by_title(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["title"]: p for p in doc["plans"]}


class FakeGh:
    """Stands in for ``gh`` behind the module's one child-process seam; every other child runs for real."""

    def __init__(
        self,
        prs: dict[str, dict[str, Any]],
        login: str = MEMBER_A_GH,
        fail: set[str] | None = None,
    ) -> None:
        self.prs = prs
        self.login = login
        self.fail = fail or set()
        self.calls: list[list[str]] = []
        self.real = plans_today.run

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[Any]:
        if Path(str(argv[0])).name != "gh":
            return self.real(argv, **kwargs)
        self.calls.append(list(argv))
        if argv[1:3] == ["api", "user"]:
            return subprocess.CompletedProcess(argv, 0, self.login + "\n", "")
        assert argv[1:3] == ["pr", "view"], argv
        key = f"{argv[argv.index('--repo') + 1].split('/')[1]}#{argv[3]}"
        if key in self.fail or key not in self.prs:
            return subprocess.CompletedProcess(
                argv, 1, "", "GraphQL: Could not resolve to a PullRequest"
            )
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.prs[key]), "")

    @property
    def pr_views(self) -> list[list[str]]:
        return [c for c in self.calls if c[1:3] == ["pr", "view"]]


def gh_pr(
    state: str = "OPEN", red: bool = False, updated: str = "2026-09-29T15:00:00Z"
) -> dict[str, Any]:
    """A ``gh pr view --json`` answer."""
    rollup = [
        {
            "__typename": "CheckRun",
            "name": "ci",
            "status": "COMPLETED",
            "conclusion": "FAILURE" if red else "SUCCESS",
        }
    ]
    return {
        "state": state,
        "isDraft": False,
        "updatedAt": updated,
        "mergedAt": updated if state == "MERGED" else None,
        "title": "x",
        "url": "u",
        "statusCheckRollup": rollup,
    }


def fake_plugin(root: Path, ledger_rows: str) -> Path:
    """An omni plugin root with a ledger reader that prints ``ledger_rows`` and a MSG executor that accepts."""
    scripts = root / "plugin" / "scripts"
    scripts.mkdir(parents=True)
    rows_file = root / "plugin-ledger-rows.txt"
    rows_file.write_text(ledger_rows, encoding="utf-8")
    (scripts / "onex_ledger.py").write_text(
        f"import sys\nsys.stdout.write(open({str(rows_file)!r}).read())\n",
        encoding="utf-8",
    )
    msg_dir = root / "plugin-msg"
    (scripts / "skill_exec.py").write_text(
        "import pathlib, sys\n"
        f"d = pathlib.Path({str(msg_dir)!r})\n"
        "if sys.argv[1] == 'newdir':\n"
        "    d.mkdir(parents=True, exist_ok=True)\n"
        "    print(d)\n"
        "else:\n"
        "    print('OK MSG fake')\n",
        encoding="utf-8",
    )
    return root / "plugin"


PYTHON = sys.executable
