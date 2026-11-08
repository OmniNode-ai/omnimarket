# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab fill reads a pull request through the repository state workflow (OMN-23033).

The control: the watcher state the lab fill used to read directly is stale and does not carry a
hold label that the PR watcher's newer tick does. The direct read misses the hold; the workflow
refreshes the watcher state first and sees it, with no GitHub-bound git command.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.ticket_query.model_ticket_facts import ModelTicketFactsOutput
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_adapters import (
    LocalLiveChecks,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_repo_state import (
    HOST_ENV,
    OWNER_ENV,
    TIMEOUT_ENV,
    RepoStateCliReader,
)
from omnimarket.nodes.node_repo_state_query_effect.models.model_repo_state_query import (
    EnumRepoStateWant,
    ModelRepoStateQuery,
)
from tests.nodes.node_repo_state_query_effect.planted import HOST, Planted, plant

STALE = "2026-11-07T21:00:00Z"


class _Facts:
    def read(self, ticket: str) -> ModelTicketFactsOutput:
        return ModelTicketFactsOutput(
            identifier=ticket, state_type="started", assignee_id=""
        )


def _state(labels: list[str], tick: str) -> dict[str, Any]:
    return {
        "last_tick": tick,
        "prs": {
            "widgets#7": {
                "facts": {"repo": "OmniNode-ai/widgets", "number": 7, "labels": labels}
            }
        },
    }


def _planted(tmp_path: Path) -> Planted:
    """A stale watcher state without the hold, a bus event newer than it, a refresh that adds it."""
    p = plant(tmp_path, watcher_tick=STALE)
    p.watcher_path.write_text(json.dumps(_state([], STALE)))
    p.bus_merge("b" * 40, pr=7, at=datetime.now(UTC))

    def tick(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        p.watcher_runs.append(list(argv))
        p.watcher_path.write_text(
            json.dumps(_state(["hold:operator"], datetime.now(UTC).isoformat()))
        )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    p.handler._puller._run = tick
    return p


def _cli(p: Planted) -> Any:
    """`onex repo-state query` as the lab fill would run it, answered by the planted handler."""

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        opts = dict(zip(argv[3::2], argv[4::2], strict=False))
        answer = asyncio.run(
            p.handler.handle(
                ModelRepoStateQuery(
                    host=opts["--host"],
                    repo=opts["--repo"],
                    want=EnumRepoStateWant(opts["--want"]),
                    pr_number=int(opts["--pr"]),
                )
            )
        )
        return subprocess.CompletedProcess(
            argv, 0 if answer.ok else 1, stdout=answer.model_dump_json(), stderr=""
        )

    return run


def _checks(p: Planted, host: str = HOST) -> LocalLiveChecks:
    reader = RepoStateCliReader(
        host=host, owner="OmniNode-ai", timeout_seconds=30, run=_cli(p)
    )
    return LocalLiveChecks(facts=_Facts(), repo_state=reader)


def _skip(checks: LocalLiveChecks) -> tuple[str, str]:
    return checks.skip_reason(
        ticket="OMN-1",
        kind="other",
        pr="widgets#7",
        operator_id="",
        ledger_path="unused",
    )


def test_control_the_direct_watcher_read_misses_the_hold_on_a_stale_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _planted(tmp_path)
    monkeypatch.setenv("ONEX_PR_WATCHER_STATE", str(p.watcher_path))
    legacy = LocalLiveChecks._pr_hold("widgets#7")
    assert legacy == ("", "")  # the stale state carries no hold: the lane would launch


def test_golden_the_workflow_refreshes_then_reads_the_hold_with_no_github_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _planted(tmp_path)
    checks = _checks(p)
    monkeypatch.setattr(LocalLiveChecks, "_fences", staticmethod(lambda _l: set()))
    assert _skip(checks) == ("held", "hold:operator")
    assert p.runner.github_calls == 0
    assert len(p.watcher_runs) == 1


def test_error_chain_a_refused_query_is_unreadable_never_a_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _planted(tmp_path)
    checks = _checks(p, host="some-other-host")
    monkeypatch.setattr(LocalLiveChecks, "_fences", staticmethod(lambda _l: set()))
    reason, detail = _skip(checks)
    assert reason == "pr-state-unreadable"
    assert "addressed to" in detail


def test_the_reader_exists_only_on_a_host_that_declares_it() -> None:
    assert RepoStateCliReader.from_environment({}) is None
    reader = RepoStateCliReader.from_environment(
        {HOST_ENV: "h1", OWNER_ENV: "o", TIMEOUT_ENV: "30"}
    )
    assert reader is not None
