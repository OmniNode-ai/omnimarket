# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""plans-today in a member's session: only the member's tasks are ranked and dispatched.

Every other task is listed as ``other member: state not read``, and the output and its schema name
the mode, the scope and the pull request source.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_plans_today_effect.handlers import (
    handler_plans_today as plans_today,
)
from omnimarket.nodes.node_plans_today_effect.models import PLANS_TODAY_JSON_SCHEMA
from tests.nodes.node_plans_today_effect.conftest import (
    MEMBER_B_PRINCIPAL,
    SHARED,
    body,
    by_title,
    gh_for_member,
    invoke,
    issue,
    member_fixture,
    row,
    tasks_of,
)

pytestmark = pytest.mark.unit


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
