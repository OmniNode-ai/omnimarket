# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the closer reads and writes: Linear (tickets, comments, state), dod_verify (checks), a declared ledger reader (readings).

Each source is behind :class:`ProtocolSprintCloserSources` so the workflow test can run with a fake.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from omnimarket.nodes.node_sprint_closer_effect.handlers.handler_sprint_closer import (
    FINGERPRINT_PREFIX,
    HandlerSprintCloser,
)
from omnimarket.nodes.node_sprint_closer_effect.models.model_sprint_closer import (
    EnumCheckStatus,
    EnumCloserAction,
    ModelCheckResult,
    ModelSprintCloserPlan,
    ModelTicketEvidence,
    ModelTicketOutcome,
)

LINEAR_URL = "https://api.linear.app/graphql"
_CRITERION = re.compile(r"^\s*[-*]\s*\[( |x|X)\]\s*\**\s*(AC\d+)\b", re.MULTILINE)
_FP = re.compile(re.escape(FINGERPRINT_PREFIX) + r"([0-9a-f]{16})")


def parse_criteria(description: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """AC labels on checkbox lines, and those ticked. A label repeated in the text counts once."""
    labels: list[str] = []
    ticked: list[str] = []
    for mark, label in _CRITERION.findall(description or ""):
        if label not in labels:
            labels.append(label)
        if mark.lower() == "x" and label not in ticked:
            ticked.append(label)
    return tuple(labels), tuple(ticked)


def last_fingerprint(comment_bodies: list[str]) -> str | None:
    """The newest closer fingerprint among the comments (newest first)."""
    for body in comment_bodies:
        m = _FP.search(body or "")
        if m:
            return m.group(1)
    return None


def map_dod_checks(
    state: dict[str, object],
) -> tuple[str | None, str | None, tuple[ModelCheckResult, ...]]:
    """dod_verify's state JSON -> (contract ref, contract error, checks). verified is a pass, failed a fail."""
    raw = state.get("checks")
    checks: list[ModelCheckResult] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", ""))
        mapped = {"verified": EnumCheckStatus.PASS, "failed": EnumCheckStatus.FAIL}.get(
            status, EnumCheckStatus.NOT_RUN
        )
        detail = " ".join(str(item.get("message") or "").split())[:300]
        binds = item.get("binds_ac")
        checks.append(
            ModelCheckResult(
                evidence_id=str(item.get("evidence_id", "")),
                status=mapped,
                binds=tuple(str(b) for b in binds) if isinstance(binds, list) else (),
                detail="" if mapped is EnumCheckStatus.PASS else detail,
            )
        )
    if checks and all(c.evidence_id == "contract" for c in checks):
        # dod_verify found no contract to run: its checks are absent, not failing.
        return None, checks[0].detail or "dod_verify found no contract", ()
    repo = state.get("contract_repository")
    sha = str(state.get("contract_commit_sha") or "")[:12]
    path = state.get("contract_repo_path")
    ref = f"{repo}@{sha}:{path}" if repo else None
    return ref, None, tuple(checks)


def read_seen_running(
    ticket_id: str, ledger_read_argv: tuple[str, ...]
) -> tuple[str, ...]:
    """Ledger rows that name the ticket in a ticket= cell and read SEEN RUNNING (or SEEN_RUNNING).

    The rows come from the ledger reader the host declares in ``ONEX_SPRINT_CLOSER_LEDGER_READ_ARGV`` (a JSON argv that
    prints rows on stdout); with none declared the closer sees no readings and leaves tickets open.
    """
    if not ledger_read_argv:
        return ()
    proc = subprocess.run(
        list(ledger_read_argv),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ledger reader exited {proc.returncode}: {proc.stderr.strip()[-300:]}"
        )
    hits: list[str] = []
    cell = f"ticket={ticket_id}"
    for line in proc.stdout.splitlines():
        if ("SEEN RUNNING" in line or "SEEN_RUNNING" in line) and any(
            c.strip() == cell for c in line.split("|")
        ):
            hits.append(f"ledger row {line.split('|', 1)[0].strip()}")
    return tuple(hits)


class ProtocolSprintCloserSources(Protocol):
    def list_ticket_ids(self, plan: ModelSprintCloserPlan) -> list[str]: ...

    def run_ticket(
        self, plan: ModelSprintCloserPlan, ticket_id: str
    ) -> ModelTicketOutcome: ...


@dataclass
class LinearClient:
    api_key: str
    _done_cache: dict[str, str] = field(default_factory=dict)

    def gql(self, query: str, variables: dict[str, object]) -> dict[str, object]:
        req = urllib.request.Request(
            LINEAR_URL,
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={"Authorization": self.api_key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = cast(dict[str, object], json.load(resp))
        if body.get("errors"):
            raise RuntimeError(f"linear: {body['errors']}")
        return cast(dict[str, object], body["data"])

    def open_issue_ids(self, project_id: str) -> list[str]:
        q = (
            "query($id:String!,$after:String){project(id:$id){issues(first:100,after:$after,"
            'filter:{state:{type:{nin:["completed","canceled"]}}}){pageInfo{hasNextPage endCursor} '
            "nodes{identifier}}}}"
        )
        out: list[str] = []
        after: str | None = None
        while True:
            data = self.gql(q, {"id": project_id, "after": after})
            issues = cast(dict[str, dict[str, object]], data["project"])["issues"]
            out += [
                str(n["identifier"])
                for n in cast(list[dict[str, object]], issues["nodes"])
            ]
            page = cast(dict[str, object], issues["pageInfo"])
            if not page["hasNextPage"]:
                return sorted(out)
            after = str(page["endCursor"])

    def read_issue(self, ticket_id: str) -> dict[str, object]:
        q = (
            "query($id:String!){issue(id:$id){id identifier title description state{name type} "
            "assignee{name} team{id states{nodes{id name type}}} "
            "comments(first:100,orderBy:createdAt){nodes{body createdAt}}}}"
        )
        return cast(dict[str, object], self.gql(q, {"id": ticket_id})["issue"])

    def comment(self, issue_uuid: str, body: str) -> None:
        q = "mutation($i:String!,$b:String!){commentCreate(input:{issueId:$i,body:$b}){success}}"
        self.gql(q, {"i": issue_uuid, "b": body})

    def set_state(self, issue_uuid: str, state_id: str) -> None:
        q = "mutation($i:String!,$s:String!){issueUpdate(id:$i,input:{stateId:$s}){success}}"
        self.gql(q, {"i": issue_uuid, "s": state_id})


@dataclass
class DodVerifyRunner:
    """Runs node_dod_verify as a process, first from the registry root, then from the contract's own clone."""

    home_root: Path
    python: str = sys.executable
    extra_pythonpath: str | None = None
    timeout_seconds: int = 900

    def _run(
        self, ticket_id: str, cwd: Path
    ) -> tuple[int, dict[str, object] | None, str]:
        env = dict(os.environ)
        if self.extra_pythonpath:
            env["PYTHONPATH"] = self.extra_pythonpath
        proc = subprocess.run(
            [
                self.python,
                "-m",
                "omnimarket.nodes.node_dod_verify",
                "--ticket-id",
                ticket_id,
                "--execution-audience",
                "local_done_gate",
                "--dry-run",
            ],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        try:
            state = cast(dict[str, object], json.loads(proc.stdout))
        except json.JSONDecodeError:
            state = None
        return (
            proc.returncode,
            state,
            (proc.stderr or "").strip().splitlines()[-1:][0]
            if proc.stderr.strip()
            else "",
        )

    def run(
        self, ticket_id: str
    ) -> tuple[str | None, str | None, tuple[ModelCheckResult, ...]]:
        rc, state, err = self._run(ticket_id, self.home_root)
        if state is None:
            return None, (err or f"dod_verify exited {rc} with no result")[:300], ()
        repo = str(state.get("contract_repository") or "")
        clone = self.home_root / repo.split("/")[-1] if repo else None
        if clone is not None and clone.is_dir():
            _rc2, state2, _ = self._run(ticket_id, clone)
            if state2 is not None:
                state = state2
        return map_dod_checks(state)


@dataclass
class LiveSprintCloserSources:
    """The production sources: Linear for tickets and writes, dod_verify for checks, the ledger for readings."""

    linear: LinearClient
    dod: DodVerifyRunner
    ledger_read_argv: tuple[str, ...] = ()

    def list_ticket_ids(self, plan: ModelSprintCloserPlan) -> list[str]:
        if plan.ticket_ids:
            return list(plan.ticket_ids)
        return self.linear.open_issue_ids(plan.project_id)

    def run_ticket(
        self, plan: ModelSprintCloserPlan, ticket_id: str
    ) -> ModelTicketOutcome:
        issue = self.linear.read_issue(ticket_id)
        state = cast(dict[str, str], issue["state"])
        assignee = cast(dict[str, str] | None, issue.get("assignee"))
        name = assignee["name"] if assignee else None
        criteria, ticked = parse_criteria(str(issue.get("description") or ""))
        bodies = [
            str(c["body"])
            for c in cast(dict[str, list[dict[str, object]]], issue["comments"])[
                "nodes"
            ]
        ]
        bodies.reverse()  # newest first
        if state.get("type") in ("completed", "canceled"):
            return ModelTicketOutcome(
                ticket_id=ticket_id,
                assignee=name,
                verdict="leave",
                action=EnumCloserAction.NO_CHANGE,
                missing=(f"already {state.get('name')}",),
                dry_run=plan.dry_run,
            )
        contract_ref, contract_error, checks = self.dod.run(ticket_id)
        if not checks and contract_error is None:
            contract_error = "dod_verify reported no checks"
        evidence = ModelTicketEvidence(
            ticket_id=ticket_id,
            title=str(issue.get("title") or ""),
            state=str(state.get("name") or ""),
            assignee=name,
            is_collaborator=name is not None and name not in plan.owner_assignees,
            criteria=criteria,
            criteria_ticked=ticked,
            contract_ref=contract_ref,
            contract_error=contract_error,
            checks=checks,
            seen_running=read_seen_running(ticket_id, self.ledger_read_argv),
        )
        decision = HandlerSprintCloser(plan.actor, last_fingerprint(bodies)).handle(
            evidence
        )
        commented = False
        state_set: str | None = None
        if not plan.dry_run and decision.comment:
            self.linear.comment(str(issue["id"]), decision.comment)
            commented = True
        if not plan.dry_run and decision.action is EnumCloserAction.SET_DONE:
            team = cast(dict[str, dict[str, list[dict[str, str]]]], issue["team"])
            done = [
                s
                for s in team["states"]["nodes"]
                if s["type"] == "completed" and s["name"] == "Done"
            ]
            if not done:
                raise RuntimeError(f"{ticket_id}: the team has no Done state")
            self.linear.set_state(str(issue["id"]), done[0]["id"])
            state_set = "Done"
        return ModelTicketOutcome(
            ticket_id=ticket_id,
            assignee=name,
            verdict=decision.eligibility.verdict,
            action=decision.action,
            missing=decision.eligibility.missing,
            evidence=decision.eligibility.evidence,
            commented=commented,
            state_set=state_set,
            dry_run=plan.dry_run,
        )


def live_sources_from_env(home_root: Path) -> LiveSprintCloserSources:
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        raise RuntimeError("LINEAR_API_KEY is not set in the worker's environment")
    declared = os.environ.get("ONEX_SPRINT_CLOSER_LEDGER_READ_ARGV", "").strip()
    return LiveSprintCloserSources(
        linear=LinearClient(key),
        dod=DodVerifyRunner(
            home_root=home_root,
            python=os.environ.get("ONEX_SPRINT_CLOSER_DOD_PYTHON", sys.executable),
            extra_pythonpath=os.environ.get("ONEX_SPRINT_CLOSER_DOD_PYTHONPATH"),
        ),
        ledger_read_argv=tuple(str(a) for a in json.loads(declared))
        if declared
        else (),
    )
