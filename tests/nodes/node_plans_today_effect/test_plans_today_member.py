# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""plans-today in a member's session: role resolution, member scope, the member PR source.

Every run resolves the caller through the team identity declaration from the Linear subject the
snapshot's sidecar records. A member's run ranks only the member's tasks, reads their pull requests
with ``gh pr view`` (capped, refusing on a failed read), never reads the watcher, and lists every
other task as ``other member: state not read``. Unknown identity never runs the operator's mode.
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
from omnimarket.nodes.node_plans_today_effect.models import PLANS_TODAY_JSON_SCHEMA
from tests.nodes.node_plans_today_effect.conftest import (
    MEMBER_A_GH,
    MEMBER_A_LINEAR,
    MEMBER_A_PRINCIPAL,
    MEMBER_B_LINEAR,
    MEMBER_B_PRINCIPAL,
    OPERATOR_LINEAR,
    UNKNOWN_LINEAR,
    FakeGh,
    body,
    by_title,
    fake_plugin,
    gh_pr,
    invoke,
    issue,
    ledger,
    make_fixture,
    pr,
    row,
)

pytestmark = pytest.mark.unit

URL = "https://github.com/OmniNode-ai/{repo}/pull/{n}"

# A plan shared by two members: T1 and T3 are member A's (assigned; claimed), T2 is member B's.
SHARED = {
    "2026-09-28-shared.md": body(
        "Shared",
        [("mine", "OMN-2001"), ("theirs", "OMN-2002"), ("claimed", "OMN-2003")],
    )
}


def member_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    plans: dict[str, str] | None = None,
    sidecar_issues: list[dict[str, Any]] | None = None,
    rows: list[str] | None = None,
    subject: str | None = MEMBER_A_LINEAR,
    origin: bool = False,
) -> dict[str, Any]:
    """Member A's session: no watcher file, member B's red PR in the shared plan."""
    return make_fixture(
        tmp_path,
        monkeypatch,
        plans or SHARED,
        rows=rows
        if rows is not None
        else [
            row(
                1,
                "2026-09-29T15:50:00Z",
                "CLAIM",
                "lane-a",
                "OMN-2003",
                principal=MEMBER_A_PRINCIPAL,
                pr_cell="omnimarket#12",
            ),
            row(
                2,
                "2026-09-29T15:40:00Z",
                "CLAIM",
                "lane-b",
                "OMN-2002",
                principal=MEMBER_B_PRINCIPAL,
                pr_cell="omnimarket#99",
            ),
        ],
        subject=subject,
        sidecar_issues=sidecar_issues
        if sidecar_issues is not None
        else [
            issue("OMN-2001", prs=[URL.format(repo="omnimarket", n=11)]),
            issue("OMN-2003", assignee=MEMBER_B_LINEAR, prs=[]),
        ],
        watcher=False,
        origin=origin,
    )


def gh_for_member(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> FakeGh:
    fake = FakeGh(
        kwargs.pop(
            "prs",
            {
                "omnimarket#11": gh_pr(red=False),
                "omnimarket#12": gh_pr(red=True),
                "omnimarket#99": gh_pr(red=True),
            },
        ),
        **kwargs,
    )
    monkeypatch.setattr(plans_today, "run", fake)
    return fake


def tasks_of(doc: dict[str, Any], title: str) -> dict[str, dict[str, Any]]:
    return {t["label"]: t for t in by_title(doc)[title]["tasks"]}


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


# --------------------------------------------------------------------------- (a), (c), (e) scope


def test_only_the_members_tasks_are_ranked_and_the_others_are_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = member_fixture(tmp_path, monkeypatch)
    gh_for_member(monkeypatch)
    code, doc = invoke(fx, watcher=False)
    assert code == 0
    assert doc is not None
    assert doc["scope"]["tickets"] == ["OMN-2001", "OMN-2003"]
    assert doc["scope"]["assigned"] == ["OMN-2001"]
    assert doc["scope"]["claimed"] == ["OMN-2003"]
    tasks = tasks_of(doc, "Shared")
    assert tasks["T1"]["state"] == "open-green"
    assert tasks["T1"]["in_scope"]
    assert tasks["T3"]["state"] == "open-red"
    assert tasks["T3"]["in_scope"]
    assert tasks["T3"]["state_basis"] == "gh-open-pr"
    theirs = tasks["T2"]
    assert theirs["in_scope"] is False
    assert theirs["state"] == "unread"
    assert theirs["state_basis"] == "other member: state not read"
    assert theirs["owner"] == "other member: state not read"
    assert theirs["prs"] == []
    plan = by_title(doc)["Shared"]
    assert plan["counts"]["unread"] == 1
    assert plan["next_action_ticket"] in ("OMN-2001", "OMN-2003")
    detail = (fx["out"] / "detail.md").read_text()
    assert "| T2 | OMN-2002 | - | other member: state not read |" in detail


def test_a_plan_holding_only_other_members_tasks_is_not_ranked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plans = {
        **SHARED,
        "2026-09-28-theirs.md": body("Theirs", [("x", "OMN-2002")]),
    }
    fx = member_fixture(tmp_path, monkeypatch, plans=plans)
    gh_for_member(monkeypatch)
    code, doc = invoke(fx, watcher=False)
    assert code == 0
    assert doc is not None
    assert set(by_title(doc)) == {"Shared"}


def test_another_members_open_red_pr_is_never_read_or_dispatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plans = {
        "2026-09-28-red.md": body("Red", [("theirs", "OMN-2002"), ("mine", "OMN-2001")])
    }
    fx = member_fixture(
        tmp_path,
        monkeypatch,
        plans=plans,
        sidecar_issues=[issue("OMN-2001", prs=[])],
        rows=[
            row(
                1,
                "2026-09-29T15:40:00Z",
                "CLAIM",
                "lane-b",
                "OMN-2002",
                principal=MEMBER_B_PRINCIPAL,
                pr_cell="omnimarket#99",
            )
        ],
    )
    fake = gh_for_member(monkeypatch)
    code, doc = invoke(fx, watcher=False)
    assert code == 0
    assert doc is not None
    plan = by_title(doc)["Red"]
    assert plan["next_action_ticket"] == "OMN-2001"
    assert plan["next_action_kind"] == "dispatch"
    assert tasks_of(doc, "Red")["T1"]["state"] == "unread"
    assert fake.pr_views == []


# --------------------------------------------------------------------------- (b), (d) PR source


def test_scoped_prs_are_the_union_of_linear_attachments_and_the_members_pr_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = member_fixture(tmp_path, monkeypatch)
    fake = gh_for_member(monkeypatch)
    code, doc = invoke(fx, watcher=False)
    assert code == 0
    assert doc is not None
    read = sorted(c[3] for c in fake.pr_views)
    assert read == ["11", "12"], (
        "one gh pr view per scoped PR; the other member's #99 unread"
    )
    assert all(
        c[c.index("--repo") + 1] == "OmniNode-ai/omnimarket" for c in fake.pr_views
    )
    assert ["gh", "api", "user", "--jq", ".login"] in fake.calls
    assert not any("search" in c for c in fake.calls)
    assert doc["sources"]["pr_reads"] == 2
    assert doc["sources"]["pr_read_cap"] == 30
    assert doc["sources"]["watcher_last_tick"] == "not read (member mode)"
    tasks = tasks_of(doc, "Shared")
    assert tasks["T1"]["prs"] == ["omnimarket#11"]
    assert tasks["T3"]["prs"] == ["omnimarket#12"]


def test_scoped_ticket_whose_attachments_were_not_read_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fx = member_fixture(
        tmp_path,
        monkeypatch,
        sidecar_issues=[issue("OMN-2001", prs=None), issue("OMN-2003", prs=[])],
    )
    fake = gh_for_member(monkeypatch)
    code, doc = invoke(fx, watcher=False)
    assert code == 2
    assert doc is None
    assert "OMN-2001" in capsys.readouterr().err
    assert fake.calls == []


def test_more_than_thirty_prs_refuses_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    urls = [URL.format(repo="omnimarket", n=n) for n in range(100, 131)]
    fx = member_fixture(
        tmp_path,
        monkeypatch,
        sidecar_issues=[issue("OMN-2001", prs=urls), issue("OMN-2003", prs=[])],
        rows=[],
    )
    fake = gh_for_member(monkeypatch, prs={})
    code, doc = invoke(fx, watcher=False)
    assert code == 2
    assert doc is None
    assert "more than the 30 reads" in capsys.readouterr().err
    assert fake.calls == []


def test_thirty_prs_are_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = [URL.format(repo="omnimarket", n=n) for n in range(100, 130)]
    fx = member_fixture(
        tmp_path,
        monkeypatch,
        sidecar_issues=[issue("OMN-2001", prs=urls), issue("OMN-2003", prs=[])],
        rows=[],
    )
    fake = gh_for_member(
        monkeypatch,
        prs={f"omnimarket#{n}": gh_pr(state="MERGED") for n in range(100, 130)},
    )
    code, doc = invoke(fx, watcher=False)
    assert code == 0
    assert doc is not None
    assert len(fake.pr_views) == 30
    assert doc["sources"]["pr_reads"] == 30


def test_a_failed_read_refuses_with_the_pr_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fx = member_fixture(tmp_path, monkeypatch)
    gh_for_member(
        monkeypatch,
        prs={"omnimarket#11": gh_pr(), "omnimarket#12": gh_pr()},
        fail={"omnimarket#12"},
    )
    code, doc = invoke(fx, watcher=False)
    assert code == 2
    assert doc is None
    assert "omnimarket#12" in capsys.readouterr().err


def test_reads_run_only_under_the_members_own_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fx = member_fixture(tmp_path, monkeypatch)
    fake = gh_for_member(monkeypatch, login="fixture-member-b-gh")
    code, doc = invoke(fx, watcher=False)
    assert code == 2
    assert doc is None
    assert "another GitHub login" in capsys.readouterr().err
    assert fake.pr_views == []


# --------------------------------------------------------------------------- (e2)


def test_assigned_ticket_with_only_a_linear_attached_red_pr_is_in_progress_and_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plans = {"2026-09-28-e2.md": body("E2", [("mine", "OMN-2201")])}
    fx = member_fixture(
        tmp_path,
        monkeypatch,
        plans=plans,
        sidecar_issues=[
            issue(
                "OMN-2201",
                status="In Progress",
                prs=[URL.format(repo="omnibase_core", n=7)],
            )
        ],
        rows=[],
    )
    gh_for_member(monkeypatch, prs={"omnibase_core#7": gh_pr(red=True)})
    code, doc = invoke(fx, watcher=False)
    assert code == 0
    assert doc is not None
    plan = by_title(doc)["E2"]
    assert plan["tasks"][0]["state"] == "open-red"
    assert plan["tasks"][0]["prs"] == ["omnibase_core#7"]
    assert plan["verdict"] != "NOT-STARTED"
    assert plan["next_action_kind"] == "dispatch-fix"


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


# --------------------------------------------------------------------------- output names scope and source


def test_output_names_its_scope_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fx = member_fixture(tmp_path, monkeypatch)
    gh_for_member(monkeypatch)
    code, _ = invoke(fx, watcher=False)
    out = capsys.readouterr().out
    assert code == 0
    assert (
        "scope: member fixture-member-a: 2 tickets (1 assigned, 1 on open claims)"
        in out
    )
    assert "source: gh pr view under the member's login (2 of at most 30 reads)" in out
    assert "other member: state not read" in out
    detail = (fx["out"] / "detail.md").read_text()
    assert "- mode: member; scope: member fixture-member-a" in detail
    assert "- pull request source: gh pr view" in detail


def _schema_findings(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """A small JSON-schema walker: type, enum, const, required, additionalProperties, items."""
    out: list[str] = []
    types = schema.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        py = {
            "object": dict,
            "array": list,
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "null": type(None),
        }
        if not any(isinstance(value, py[n]) for n in names):
            out.append(f"{path}: {value!r} is not {names}")
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: {value!r} not in {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: {value!r} != {schema['const']!r}")
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                out.append(f"{path}: lacks {k}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            out += [f"{path}: extra {k}" for k in value if k not in props]
        for k, sub in props.items():
            if k in value:
                out += _schema_findings(value[k], sub, f"{path}.{k}")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            out += _schema_findings(v, schema["items"], f"{path}[{i}]")
    return out


def test_schema_fields_name_the_scope_and_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = PLANS_TODAY_JSON_SCHEMA
    assert {"mode", "scope"} <= set(schema["required"])
    assert {"pr_source", "pr_reads", "pr_read_cap", "team_identity"} <= set(
        schema["properties"]["sources"]["required"]
    )
    task = schema["properties"]["plans"]["items"]["properties"]["tasks"]["items"]
    assert "unread" in task["properties"]["state"]["enum"]
    assert "in_scope" in task["required"]
    fx = member_fixture(tmp_path, monkeypatch)
    gh_for_member(monkeypatch)
    code, doc = invoke(fx, watcher=False)
    assert code == 0
    assert doc is not None
    assert _schema_findings(doc, schema) == []
    assert plans_today.validate(doc) == []
    bad = json.loads(json.dumps(doc))
    t2 = next(t for t in bad["plans"][0]["tasks"] if t["label"] == "T2")
    t2["state"] = "not-started"
    assert any("outside the scope" in f for f in plans_today.validate(bad))
    bad = json.loads(json.dumps(doc))
    bad["plans"][0]["next_action_ticket"] = "OMN-2002"
    assert any("must not be dispatched" in f for f in plans_today.validate(bad))
    bad = json.loads(json.dumps(doc))
    bad["sources"]["pr_source"] = "pr-watcher"
    assert any("pr_source" in f for f in plans_today.validate(bad))


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
