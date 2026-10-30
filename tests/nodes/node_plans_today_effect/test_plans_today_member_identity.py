# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""plans-today in a member's session: the role is resolved on every run, with no new argument.

The Linear subject the snapshot's sidecar records is resolved through the team identity declaration.
Only an authenticated operator runs the operator's mode, which still refuses a missing or stale
watcher; a member runs member mode; anything else refuses, never the operator's mode.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_plans_today_effect import (
    plans_today_member as member,
)
from omnimarket.nodes.node_plans_today_effect.handlers import (
    handler_plans_today as plans_today,
)
from tests.nodes.node_plans_today_effect.conftest import (
    MEMBER_A_GH,
    MEMBER_A_LINEAR,
    MEMBER_B_LINEAR,
    OPERATOR_LINEAR,
    SHARED,
    UNKNOWN_LINEAR,
    URL,
    FakeGh,
    body,
    by_title,
    fake_plugin,
    gh_for_member,
    invoke,
    issue,
    ledger,
    make_fixture,
    member_fixture,
    pr,
    tasks_of,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- (0) role resolution


def test_member_mode_is_resolved_with_no_new_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = member_fixture(tmp_path, monkeypatch)
    gh_for_member(monkeypatch)
    code, doc = invoke(fx, watcher=False)
    assert code == 0
    assert doc is not None
    assert doc["mode"] == "member"
    assert doc["scope"]["kind"] == "member"
    assert doc["scope"]["member"] == "fixture-member-a"
    assert doc["sources"]["pr_source"] == "gh-pr-view"
    assert "sha256=" in doc["sources"]["team_identity"]


def test_role_is_resolved_on_every_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = member_fixture(tmp_path, monkeypatch)
    gh_for_member(monkeypatch)
    calls: list[str] = []
    real = member.HandlerTeamIdentity.handle

    def spy(self: Any, request: Any) -> Any:
        calls.append(request.claims[0].value)
        return real(self, request)

    monkeypatch.setattr(member.HandlerTeamIdentity, "handle", spy)
    invoke(fx, watcher=False)
    invoke(fx, watcher=False)
    assert calls == [MEMBER_A_LINEAR, MEMBER_A_LINEAR]


def test_hook_call_runs_member_mode_with_no_watcher_and_leaves_the_other_red_pr_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session-start hook's exact call on an opted-in resume: today's snapshot, no watcher file."""
    fx = member_fixture(tmp_path, monkeypatch, origin=True)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    plans_dir = tmp_path / ".claude_scratch" / "plans-today"
    plans_dir.mkdir(parents=True)
    snapshot = plans_dir / f"linear-{today}.txt"
    snapshot.write_text(fx["linear"].read_text())
    snapshot.with_suffix(".json").write_text(fx["sidecar"].read_text())
    plugin = fake_plugin(tmp_path, fx["ledger"].read_text())
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(tmp_path / "ledger.md"))
    fake = gh_for_member(monkeypatch)
    assert not (tmp_path / ".claude_scratch" / "pr-watcher" / "state.json").exists()

    from omnimarket.nodes.node_plans_today_effect.runtime_plans_today import (
        main,
    )

    code = main(["--linear-snapshot", str(snapshot), "--orchestrator", "lane-o"])
    assert code == 0
    doc = json.loads((plans_dir / today / "plans-today.json").read_text())
    assert doc["mode"] == "member"
    theirs = tasks_of(doc, "Shared")["T2"]
    assert theirs["state"] == "unread"
    assert theirs["prs"] == []
    assert all("99" not in " ".join(c) for c in fake.calls), (
        "the other member's PR is never read"
    )


def test_operator_subject_runs_the_operators_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = make_fixture(
        tmp_path,
        monkeypatch,
        SHARED,
        prs={"repo#1": pr("OMN-2002")},
        subject=OPERATOR_LINEAR,
    )
    code, doc = invoke(fx)
    assert code == 0
    assert doc is not None
    assert doc["mode"] == "operator"
    assert doc["scope"]["kind"] == "all"
    assert doc["sources"]["pr_source"] == "pr-watcher"
    assert tasks_of(doc, "Shared")["T2"]["state"] == "open-red"


@pytest.mark.parametrize(
    "case",
    ["no-sidecar", "unknown-subject", "no-declaration", "literal-me", "malformed"],
)
def test_unknown_identity_never_runs_the_operators_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    fx = make_fixture(
        tmp_path,
        monkeypatch,
        SHARED,
        prs={"repo#1": pr("OMN-2002")},
        subject=UNKNOWN_LINEAR if case == "unknown-subject" else OPERATOR_LINEAR,
    )
    if case == "no-sidecar":
        fx["sidecar"].unlink()
    elif case == "no-declaration":
        monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "nowhere"))
    elif case == "literal-me":
        fx["sidecar"].write_text(json.dumps({"operator": "me", "issues": []}))
    elif case == "malformed":
        fx["sidecar"].write_text("{not json")
    fake = FakeGh({})
    monkeypatch.setattr(plans_today, "run", fake)
    code, doc = invoke(fx)
    assert code == 2, "team restrictions: refused, never the operator's watcher run"
    assert doc is None, "team restrictions: refused, never the operator's watcher run"
    assert fake.calls == []


# --------------------------------------------------------------------------- (0b) the sidecar


def test_sidecar_of_another_subject_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fx = member_fixture(tmp_path, monkeypatch)
    gh_for_member(monkeypatch)
    identity = member.resolve_identity(fx["linear"], None)
    assert identity.entry is not None
    other = identity.entry.model_copy(update={"linear_user_id": MEMBER_B_LINEAR})
    monkeypatch.setattr(
        member,
        "load_team_identity",
        lambda _roots: type(
            "R",
            (),
            {"overlay": type("O", (), {"entries": (other,)})()},
        )(),
    )
    code, doc = invoke(fx, watcher=False)
    assert code == 2
    assert doc is None
    assert "another subject" in capsys.readouterr().err


def test_sidecar_records_assignee_and_attached_prs_and_legacy_key() -> None:
    sidecar = member.ModelLinearSidecar.model_validate(
        {
            "operator": MEMBER_A_LINEAR,
            "issues": [issue("OMN-1", prs=[URL.format(repo="r", n=1)])],
        }
    )
    assert sidecar.subject == MEMBER_A_LINEAR
    assert sidecar.issues[0].assignee == MEMBER_A_LINEAR
    assert sidecar.issues[0].pull_requests == (URL.format(repo="r", n=1),)
    with pytest.raises(ValueError, match="subject"):
        member.ModelLinearSidecar.model_validate({"issues": []})
    with pytest.raises(ValueError, match="subject"):
        member.ModelLinearSidecar.model_validate({"subject": "me", "issues": []})


def test_older_backlog_task_assigned_with_no_claim_reaches_the_ranking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = member_fixture(
        tmp_path,
        monkeypatch,
        plans={"2026-06-01-old.md": body("Old", [("backlog", "OMN-2101")])},
        sidecar_issues=[issue("OMN-2101", status="Backlog", prs=[])],
        rows=[],
    )
    gh_for_member(monkeypatch)
    code, doc = invoke(fx, watcher=False)
    assert code == 0
    assert doc is not None
    old = by_title(doc)["Old"]
    assert old["selected_by"] == ["member-scope"]
    assert old["tasks"][0]["state"] == "not-started"
    assert old["tasks"][0]["in_scope"]
    assert old["next_action_ticket"] == "OMN-2101"


# --------------------------------------------------------------------------- (f) operator unchanged


def test_operators_mode_still_refuses_a_missing_or_stale_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fx = make_fixture(tmp_path, monkeypatch, SHARED, subject=OPERATOR_LINEAR)
    code, doc = invoke(fx, now="2026-09-29T18:00:00Z")
    assert code == 2
    assert doc is None
    assert "pr-watcher state is" in capsys.readouterr().err
    fx["watcher"].unlink()
    code, doc = invoke(fx)
    assert code == 2
    assert doc is None
    assert "not found" in capsys.readouterr().err


def test_handler_does_not_name_a_github_client_outside_the_member_module() -> None:
    """The operator's path spawns no GitHub client: only the member module builds a ``gh`` argv."""
    import re

    source = Path(plans_today.__file__).read_text()
    assert not re.search(r'\[\s*["\']gh["\']', source)
    assert os.path.basename(member.__file__) == "plans_today_member.py"


def test_unrelated_child_processes_still_run(tmp_path: Path) -> None:
    """The fake gh seam passes every non-gh child through."""
    fake = FakeGh({})
    result = fake(["git", "--version"])
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 0
    assert fake.calls == []
    assert fake.login == MEMBER_A_GH
    assert ledger([]).startswith("ROWS n=0")
