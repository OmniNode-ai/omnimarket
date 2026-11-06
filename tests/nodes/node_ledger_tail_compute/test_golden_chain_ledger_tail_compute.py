# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Literal source behaviors and contract-to-bus golden/error chains for ledger facts."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from omnibase_core.nodes.node_compute import NodeCompute
from pydantic import ValidationError

import omnimarket.nodes.node_ledger_tail_compute as node_package
from omnimarket.nodes.node_ledger_tail_compute.handlers.handler_ledger_tail import (
    HandlerLedgerTail,
    as_rows,
)
from omnimarket.nodes.node_ledger_tail_compute.models.model_ledger_tail import (
    ModelLedgerTailRequest,
    ModelLedgerTailResult,
    ModelLedgerTailRow,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit
NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.ledger-tail-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.ledger-tail-completed.v1"
HANDLER = HandlerLedgerTail()
NOW = "2026-10-20T12:00:00Z"


def ask(operation: str, **fields: Any) -> ModelLedgerTailResult:
    return HANDLER.handle(
        ModelLedgerTailRequest.model_validate({"operation": operation, **fields})
    )


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    assert (
        getattr(importlib.import_module(binding["module"]), binding["class"])
        is HandlerLedgerTail
    )
    assert issubclass(node_package.NodeLedgerTailCompute, NodeCompute)
    for side, model in (
        ("input_model", ModelLedgerTailRequest),
        ("output_model", ModelLedgerTailResult),
    ):
        assert (
            getattr(
                importlib.import_module(contract[side]["module"]),
                contract[side]["name"],
            )
            is model
        )
    assert {h["operation"] for h in contract["handler_routing"]["handlers"]} == {
        "parse_rows",
        "parse_line",
        "next_drain_lane",
        "closed_claims",
        "live_drains",
        "split_key",
        "pr_cells",
        "repo_cells",
        "names_branch",
        "claim_names",
        "is_live",
        "owner",
        "as_rows",
        "failed_at_head",
        "cause_attempt_rows",
        "cause_failed_at_base",
    }
    entry = metadata["entry_points"]["onex.nodes"]["node_ledger_tail_compute"]
    assert importlib.import_module(entry) is node_package
    assert metadata["capabilities"]["side_effect_class"] == "none"


def test_parse_line_first_key_wins_and_trims_only_source_punctuation() -> None:
    result = ask(
        "parse_line",
        text="2026-10-20T11:00:00Z | FRICTION | Lane=alpha.,;) | from=beta | ticket=T-1 | prose lane=wrong X-Key=value!\n",
        line=8,
    )
    assert result.value == {
        "ts": "2026-10-20T11:00:00Z",
        "type": "FRICTION",
        "fields": {"lane": "alpha", "from": "beta", "ticket": "T-1", "x-key": "value!"},
        "raw": "2026-10-20T11:00:00Z | FRICTION | Lane=alpha.,;) | from=beta | ticket=T-1 | prose lane=wrong X-Key=value!",
        "line": 8,
    }
    assert result.refusal is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "header",
        " 2026-10-20T11:00:00Z | CLAIM | lane=a",
        "2026-10-20T11:00:00Z|CLAIM| lane=a",
        "2026-10-20T11:00:00Z | claim | lane=a",
        "2026-10-20T11:00:00Z | TYPE2 | lane=a",
    ],
)
def test_parse_line_negative(text: str) -> None:
    assert ask("parse_line", text=text).value is None


def test_parse_rows_retains_physical_line_numbers_and_raw_text() -> None:
    assert ask(
        "parse_rows",
        text="# Ledger\n\n2026-10-20T11:00:00Z | CLAIM | from=alpha\nignored\n2026-10-20T11:01:00Z | STATUS | lane=beta",
    ).value == [
        {
            "ts": "2026-10-20T11:00:00Z",
            "type": "CLAIM",
            "fields": {"from": "alpha"},
            "raw": "2026-10-20T11:00:00Z | CLAIM | from=alpha",
            "line": 3,
        },
        {
            "ts": "2026-10-20T11:01:00Z",
            "type": "STATUS",
            "fields": {"lane": "beta"},
            "raw": "2026-10-20T11:01:00Z | STATUS | lane=beta",
            "line": 5,
        },
    ]
    assert ask("parse_rows", text="# No rows\n\n").value == []


@pytest.mark.parametrize(
    ("fields", "lane"),
    [({"lane": "a", "from": "b"}, "a"), ({"lane": "", "from": "b"}, "b"), ({}, "")],
)
def test_row_lane_property(fields: dict[str, str], lane: str) -> None:
    assert ModelLedgerTailRow(ts="stamp", type="CLAIM", fields=fields).lane == lane


@pytest.mark.parametrize(
    ("lane", "value", "refusal"),
    [
        ("repo-drains-84h", "repo-drains-84i", None),
        ("repo-drains-001a", "repo-drains-001b", None),
        (
            "repo-drains-84z",
            None,
            "'repo-drains-84z' ends in z: pass the successor lane explicitly",
        ),
        ("lane-a", None, "'lane-a' is not a repo-drains-<number><letter> lane"),
    ],
)
def test_next_drain_lane(lane: str, value: str | None, refusal: str | None) -> None:
    result = ask("next_drain_lane", lane=lane)
    assert result.value == value
    assert result.refusal == refusal


def test_closed_claims_includes_terminal_and_superseded_stamps() -> None:
    text = "\n".join(
        [
            "2026-10-20T11:00:00Z | CLAIM | lane=a",
            "2026-10-20T11:01:00Z | TERMINAL | lane=a closes-claim=2026-10-20T11:00:00Z@a",
            "2026-10-20T11:02:00Z | CLAIM | lane=b supersedes-claim=2026-10-20T10:00:00Z@b",
            "2026-10-20T11:03:00Z | STATUS | lane=c closes-claim=ignored",
        ]
    )
    assert ask("closed_claims", text=text).value == [
        "2026-10-20T10:00:00Z",
        "2026-10-20T11:00:00Z",
    ]
    assert (
        ask("closed_claims", text="2026-10-20T11:00:00Z | CLAIM | lane=a").value == []
    )


def test_live_drains_excludes_closed_superseded_and_terminal_lanes() -> None:
    text = "\n".join(
        [
            "2026-10-20T10:00:00Z | CLAIM | lane=repo-drains-1a",
            "2026-10-20T10:01:00Z | CLAIM | lane=repo-drains-2a",
            "2026-10-20T10:02:00Z | CLAIM | lane=repo-drains-3a supersedes-claim=2026-10-20T10:00:00Z",
            "2026-10-20T10:03:00Z | TERMINAL | lane=repo-drains-2a",
            "2026-10-20T10:04:00Z | CLAIM | from=repo-drains-4a",
            "2026-10-20T10:05:00Z | CLAIM | lane=ordinary",
        ]
    )
    assert ask("live_drains", text=text).value == [
        {
            "ts": "2026-10-20T10:04:00Z",
            "type": "CLAIM",
            "fields": {"from": "repo-drains-4a"},
            "raw": "2026-10-20T10:04:00Z | CLAIM | from=repo-drains-4a",
            "line": 5,
        },
        {
            "ts": "2026-10-20T10:02:00Z",
            "type": "CLAIM",
            "fields": {
                "lane": "repo-drains-3a",
                "supersedes-claim": "2026-10-20T10:00:00Z",
            },
            "raw": "2026-10-20T10:02:00Z | CLAIM | lane=repo-drains-3a supersedes-claim=2026-10-20T10:00:00Z",
            "line": 3,
        },
    ]
    assert (
        ask("live_drains", text="2026-10-20T11:00:00Z | CLAIM | lane=ordinary").value
        == []
    )
    assert (
        ask(
            "live_drains",
            text="2026-10-20T10:00:00Z | CLAIM | lane=repo-drains-1a\n2026-10-20T10:01:00Z | TERMINAL | lane=other closes-claim=2026-10-20T10:00:00Z",
        ).value
        == []
    )


@pytest.mark.parametrize(
    ("pr", "value"),
    [
        (" `OmniNode-ai/OmniMarket#3059` ", ["omnimarket", 3059]),
        ("market#0001", ["market", 1]),
    ],
)
def test_split_key(pr: str, value: list[Any]) -> None:
    assert ask("split_key", pr=pr).value == value


def test_split_key_negative() -> None:
    assert (
        ask("split_key", pr="market").refusal
        == "invalid literal for int() with base 10: ''"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "pr=`OmniNode-ai/omnimarket#42`, core#9, 13 | prose market#99 ruling=ledger:4198",
            [["omnimarket", 42], ["core", 9], [None, 13]],
        ),
        ("(pr=market#42); pr=43\npr=market#99", [["market", 42], [None, 43]]),
        ("mentions market#42 but no cell\npr=market#42", []),
        ("not-pr=market#42", []),
    ],
)
def test_pr_cells(text: str, expected: list[Any]) -> None:
    assert ask("pr_cells", text=text).value == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "repos=`Market, CORE` lane=a | repo=OmniNode-ai/infra; status=ok\nrepo=ignored",
            ["core", "market", "omninode-ai/infra;"],
        ),
        ("no repository cell\nrepo=market", []),
    ],
)
def test_repo_cells(text: str, expected: list[str]) -> None:
    assert ask("repo_cells", text=text).value == expected


@pytest.mark.parametrize(
    ("text", "head_ref", "expected"),
    [
        ("working on feature-branch", "feature-branch", True),
        ("feature-branch-extra", "feature-branch", False),
        ("prefix.feature-branch", "feature-branch", False),
        ("main", "main", False),
        ("feature-branch", None, False),
        ("feature+[branch]", "feature+[branch]", True),
    ],
)
def test_names_branch(text: str, head_ref: str | None, expected: bool) -> None:
    assert ask("names_branch", text=text, head_ref=head_ref).value is expected


@pytest.mark.parametrize(
    ("text", "head_ref", "expected"),
    [
        ("pr=OmniNode-ai/OmniMarket#42", None, "pr-field"),
        ("pr=MARKET#42", None, "pr-field"),
        ("repo=OmniNode-ai/market pr=42", None, "pr-field"),
        ("repos=core,omnimarket pr=41,42", None, "pr-field"),
        ("pr=core#42 | branch=feature-branch", "feature-branch", "branch"),
        ("pr=market#42 feature-branch", "feature-branch", "pr-field"),
        (
            "ticket=T-42 cause=market#42 prose market#42 worktree=/tmp/market-42",
            None,
            None,
        ),
        ("pr=core#42", None, None),
        ("pr=42", None, None),
        ("repo=core pr=42", None, None),
        ("pr=market#420", None, None),
        ("prose\npr=market#42", None, None),
        ("feature-branch-extra", "feature-branch", None),
    ],
)
def test_claim_names_exact_pr_only(
    text: str, head_ref: str | None, expected: str | None
) -> None:
    result = ask("claim_names", text=text, pr="omnimarket#42", head_ref=head_ref)
    assert result.value == expected
    assert result.refusal is None


@pytest.mark.parametrize(
    ("repo", "alias"),
    [
        ("omnibase_infra", "infra"),
        ("omnibase_core", "core"),
        ("omnimarket", "market"),
        ("onex_change_control", "occ"),
        ("omniclaude", "claude"),
        ("omnidash", "dash"),
        ("knowledge_base", "knowledge-base"),
    ],
)
def test_claim_names_all_repo_aliases(repo: str, alias: str) -> None:
    assert (
        ask("claim_names", text=f"pr={alias}#42", pr=f"{repo}#42").value == "pr-field"
    )


def test_claim_names_supplied_repo_aliases() -> None:
    plain = ask("claim_names", text="pr=ex#42", pr="example_repo#42")
    supplied = ask(
        "claim_names",
        text="pr=ex#42",
        pr="example_repo#42",
        repo_aliases={"example_repo": ["ex"]},
    )
    assert plain.value is None
    assert supplied.value == "pr-field"


@pytest.mark.parametrize(
    ("last_at", "extra", "expected"),
    [
        ("2026-10-20T11:15:00Z", {}, True),
        ("2026-10-20T11:14:59Z", {}, False),
        (None, {}, False),
        ("2026-10-20T12:00:01Z", {}, True),
        ("2026-10-20T11:59:00Z", {"live_after_seconds": 59}, False),
    ],
)
def test_is_live(last_at: str | None, extra: dict[str, Any], expected: bool) -> None:
    assert ask("is_live", last_at=last_at, now=NOW, **extra).value is expected


def test_owner_lease_precedes_claims_and_canonicalizes_keys() -> None:
    assert ask(
        "owner",
        pr="omnimarket#42",
        now=NOW,
        leases=[{"pr": "OmniNode-ai/OmniMarket#42", "holder": "worker"}],
        open_claims=[{"lane": "claimant", "at": NOW, "text": "pr=market#42"}],
    ).value == {"state": "OWNED", "by": "lease", "lane": "worker", "why": "lease"}
    assert ask(
        "owner", pr="omnimarket#42", now=NOW, leases=[{"pr": "omnimarket#42"}]
    ).value == {
        "state": "OWNED",
        "by": "lease",
        "lane": "landing-controller",
        "why": "lease",
    }
    assert ask(
        "owner", pr="omnimarket#42", now=NOW, leases=[{"pr": "core#42"}]
    ).value == {"state": "FREE", "stale": []}


def test_owner_last_live_claim_wins_and_stale_claims_are_retained() -> None:
    assert ask(
        "owner",
        pr="omnimarket#42",
        now=NOW,
        head_ref="feature-branch",
        open_claims=[
            {
                "lane": "stale",
                "ts": "old",
                "at": "2026-10-20T11:14:59Z",
                "text": "pr=market#42",
            },
            {
                "lane": "first",
                "ts": "first-stamp",
                "at": "2026-10-20T11:59:00Z",
                "text": "pr=market#42",
            },
            {
                "lane": "last",
                "ts": "last-stamp",
                "at": "2026-10-20T11:15:00Z",
                "text": "feature-branch",
            },
            {"lane": "unrelated", "at": NOW, "text": "ticket=T-42 prose market#42"},
        ],
    ).value == {
        "state": "OWNED",
        "by": "claim",
        "lane": "last",
        "claim_ts": "last-stamp",
        "why": "branch",
        "stale": [{"lane": "stale", "claim_ts": "old", "why": "pr-field"}],
    }


def test_owner_uses_max_of_claim_and_lane_clock_and_custom_ttl() -> None:
    claims = [
        {
            "lane": "a",
            "ts": "stamp",
            "at": "2026-10-20T10:00:00Z",
            "text": "pr=market#42",
        }
    ]
    assert ask(
        "owner",
        pr="omnimarket#42",
        now=NOW,
        open_claims=claims,
        last_row={"a": "2026-10-20T11:59:00Z"},
    ).value == {
        "state": "OWNED",
        "by": "claim",
        "lane": "a",
        "claim_ts": "stamp",
        "why": "pr-field",
        "stale": [],
    }
    assert ask(
        "owner",
        pr="omnimarket#42",
        now=NOW,
        open_claims=claims,
        live_after_seconds=7200,
        last_row={"a": "2026-10-20T09:00:00Z"},
    ).value == {
        "state": "OWNED",
        "by": "claim",
        "lane": "a",
        "claim_ts": "stamp",
        "why": "pr-field",
        "stale": [],
    }
    assert ask("owner", pr="omnimarket#42", now=NOW, open_claims=claims).value == {
        "state": "FREE",
        "stale": [{"lane": "a", "claim_ts": "stamp", "why": "pr-field"}],
    }
    assert ask(
        "owner", pr="omnimarket#42", now=NOW, open_claims=[{"text": "pr=market#42"}]
    ).value == {
        "state": "FREE",
        "stale": [{"lane": "", "claim_ts": None, "why": "pr-field"}],
    }


def test_as_rows_preserves_mapping_and_object_branches() -> None:
    assert ask(
        "as_rows",
        rows=[
            {
                "type": "CLAIM",
                "lane": "a",
                "ts": "stamp",
                "text": None,
                "ignored": True,
            },
            {},
        ],
    ).value == [
        {"type": "CLAIM", "lane": "a", "ts": "stamp", "text": ""},
        {"type": None, "lane": None, "ts": None, "text": ""},
    ]
    assert as_rows(
        [
            SimpleNamespace(
                rtype="STATUS", type="CLAIM", lane="a", ts="stamp", text="text"
            )
        ]
    ) == [{"type": "STATUS", "lane": "a", "ts": "stamp", "text": "text"}]
    parsed = ModelLedgerTailRow(
        ts="stamp", type="CLAIM", fields={"from": "a"}, raw="not used as text"
    )
    assert ask("as_rows", rows=[parsed]).value == [
        {"type": "CLAIM", "lane": "a", "ts": "stamp", "text": ""}
    ]
    assert ask("as_rows", rows=[]).value == []


@pytest.mark.parametrize(
    ("head", "extra", "expected"),
    [
        (
            "ABCDEF123456",
            {},
            {
                "ts": "2026-10-20T11:10:00Z",
                "lane": "a",
                "head": "abcdef1",
                "outcome": "blocked",
            },
        ),
        ("0000000", {}, None),
        (None, {}, None),
    ],
)
def test_failed_at_head_inherits_claim_pr_and_head(
    head: str | None, extra: dict[str, Any], expected: dict[str, Any] | None
) -> None:
    text = "2026-10-20T11:00:00Z | CLAIM | lane=a pr=omnimarket#42 head=AbCdEf1\n2026-10-20T11:10:00Z | TERMINAL | lane=a outcome=BLOCKED"
    assert (
        ask(
            "failed_at_head",
            text=text,
            pr="OmniNode-ai/omnimarket#42",
            head=head,
            now=NOW,
            **extra,
        ).value
        == expected
    )


def test_failed_at_head_closes_specific_claim_and_latest_attempt_decides() -> None:
    text = "\n".join(
        [
            "2026-10-20T10:00:00Z | CLAIM | lane=a pr=omnimarket#42 head=abcdef1",
            "2026-10-20T10:01:00Z | CLAIM | lane=a pr=omnimarket#99 head=1111111",
            "2026-10-20T11:00:00Z | TERMINAL | lane=a closes_claim=2026-10-20T10:00:00Z outcome=failed",
        ]
    )
    assert ask(
        "failed_at_head", text=text, pr="omnimarket#42", head="abcdef123", now=NOW
    ).value == {
        "ts": "2026-10-20T11:00:00Z",
        "lane": "a",
        "head": "abcdef1",
        "outcome": "failed",
    }
    success = "\n2026-10-20T11:30:00Z | TERMINAL | lane=a pr=omnimarket#42 head=abcdef1 outcome=merged"
    assert (
        ask(
            "failed_at_head",
            text=text + success,
            pr="omnimarket#42",
            head="abcdef1",
            now=NOW,
        ).value
        is None
    )
    other_head = "\n2026-10-20T11:30:00Z | TERMINAL | lane=b pr=omnimarket#42 head=1111111 outcome=merged"
    assert ask(
        "failed_at_head",
        text=text + other_head,
        pr="omnimarket#42",
        head="abcdef1",
        now=NOW,
    ).value == {
        "ts": "2026-10-20T11:00:00Z",
        "lane": "a",
        "head": "abcdef1",
        "outcome": "failed",
    }


def test_failed_at_head_terminal_pr_and_head_override_claim() -> None:
    text = "2026-10-20T11:00:00Z | CLAIM | lane=a pr=omnimarket#99 head=1111111\n2026-10-20T11:01:00Z | TERMINAL | lane=a pr=omnimarket#42 head=abcdef1 outcome=failed"
    assert ask(
        "failed_at_head", text=text, pr="omnimarket#42", head="abcdef1", now=NOW
    ).value == {
        "ts": "2026-10-20T11:01:00Z",
        "lane": "a",
        "head": "abcdef1",
        "outcome": "failed",
    }
    assert (
        ask(
            "failed_at_head", text=text, pr="omnimarket#99", head="1111111", now=NOW
        ).value
        is None
    )


@pytest.mark.parametrize(
    ("stamp", "now", "headless_h", "expected"),
    [
        ("2026-10-20T10:00:00Z", NOW, 2, True),
        ("2026-10-20T09:59:59Z", NOW, 2, False),
        ("2026-10-20T11:00:00Z", NOW, 0.5, False),
        ("invalid", NOW, 2, False),
        ("2026-10-20T11:00:00Z", "invalid", 2, False),
        ("2026-10-20T11:00:00", NOW, 2, True),
    ],
)
def test_failed_at_head_headless_ttl(
    stamp: str, now: str, headless_h: float, expected: bool
) -> None:
    row = {
        "type": "TERMINAL",
        "lane": "a",
        "ts": stamp,
        "text": "pr=omnimarket#42 outcome=failed",
    }
    value = ask(
        "failed_at_head",
        rows=[row],
        pr="omnimarket#42",
        head="abcdef1",
        now=now,
        headless_h=headless_h,
    ).value
    assert value == (
        {"ts": stamp, "lane": "a", "head": None, "outcome": "failed"}
        if expected
        else None
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "pr=omnimarket#42,omnimarket#43 head=abcdef1 outcome=failed",
            {
                "ts": "2026-10-20T11:00:00Z",
                "lane": "a",
                "head": None,
                "outcome": "failed",
            },
        ),
        ("pr=42 outcome=failed", None),
        ("repo=omnimarket pr=42 outcome=failed", None),
        ("pr=market#42 head=abcdef1 outcome=failed", None),
        ("mentions omnimarket#42 outcome=failed", None),
        ("pr=omnimarket#42 outcome=merged", None),
        ("pr=omnimarket#42\nhead=abcdef1 outcome=failed", None),
    ],
)
def test_failed_at_head_special_matching_rules(
    text: str, expected: dict[str, Any] | None
) -> None:
    assert (
        ask(
            "failed_at_head",
            rows=[
                {
                    "type": "TERMINAL",
                    "lane": "a",
                    "ts": "2026-10-20T11:00:00Z",
                    "text": text,
                }
            ],
            pr="omnimarket#42",
            head="1111111",
            now=NOW,
        ).value
        == expected
    )


def test_cause_attempt_rows_requires_terminal_cause_and_base_on_first_line() -> None:
    assert ask(
        "cause_attempt_rows",
        rows=[
            {
                "type": "TERMINAL",
                "lane": "a",
                "ts": "stamp",
                "text": "cause=lint base=abcdef1",
            },
            {"type": "CLAIM", "lane": "b", "text": "cause=lint base=abcdef1"},
            {"type": "TERMINAL", "text": "cause=lint"},
            {"type": "TERMINAL", "text": "base=abcdef1\ncause=lint"},
        ],
    ).value == [
        {
            "type": "TERMINAL",
            "lane": "a",
            "ts": "stamp",
            "text": "cause=lint base=abcdef1",
        }
    ]
    assert (
        ask(
            "cause_attempt_rows",
            text="2026-10-20T11:00:00Z | TERMINAL | lane=a cause=lint",
        ).value
        == []
    )


@pytest.mark.parametrize(
    ("cells", "expected"),
    [
        ("outcome=blocked", "blocked"),
        ("result=external_blocker", "external_blocker"),
        ("result=unparseable", "unparseable"),
        ("result=CAUSE_FIX_SUBMITTED", None),
        ("result=cause_not_shared", None),
        ("outcome=merged", None),
        ("", None),
        ("outcome=failed result=cause_fix_submitted", "cause_fix_submitted"),
    ],
)
def test_cause_failed_at_base_results(cells: str, expected: str | None) -> None:
    value = ask(
        "cause_failed_at_base",
        text=f"2026-10-20T11:00:00Z | TERMINAL | lane=a cause=lint base=ABCDEF1 {cells}",
        cause="lint",
        base="abcdef123",
    ).value
    assert value == (
        {
            "ts": "2026-10-20T11:00:00Z",
            "lane": "a",
            "base": "abcdef1",
            "result": expected,
        }
        if expected
        else None
    )


@pytest.mark.parametrize(
    ("cause", "base"), [("other", "abcdef1"), ("lint", None), ("lint", "1111111")]
)
def test_cause_failed_at_base_negative(cause: str, base: str | None) -> None:
    assert (
        ask(
            "cause_failed_at_base",
            text="2026-10-20T11:00:00Z | TERMINAL | lane=a cause=lint base=abcdef1 outcome=failed",
            cause=cause,
            base=base,
        ).value
        is None
    )


def test_cause_failed_at_base_answers_clear_failures_non_attempts_preserve() -> None:
    text = "2026-10-20T11:00:00Z | TERMINAL | lane=a cause=lint base=abcdef1 result=external_blocker\n2026-10-20T11:10:00Z | TERMINAL | lane=b cause=lint base=abcdef123 result=cause_not_shared"
    assert (
        ask("cause_failed_at_base", text=text, cause="lint", base="abcdef1").value
        is None
    )
    assert ask(
        "cause_failed_at_base",
        text=text,
        cause="lint",
        base="abcdef1",
        attempts=[True, False],
    ).value == {
        "ts": "2026-10-20T11:00:00Z",
        "lane": "a",
        "base": "abcdef1",
        "result": "external_blocker",
    }
    assert (
        ask(
            "cause_failed_at_base",
            text=text,
            cause="lint",
            base="abcdef1",
            attempts=[False, False],
        ).value
        is None
    )
    assert (
        ask(
            "cause_failed_at_base",
            text=text,
            cause="lint",
            base="abcdef1",
            attempts=[True],
        ).refusal
        == "attempts must have one boolean per row"
    )


@pytest.mark.parametrize("operation", ["is_live", "owner", "failed_at_head"])
def test_clock_is_never_implicit(operation: str) -> None:
    assert ask(operation, pr="omnimarket#42").refusal == (
        "now must be supplied"
        if operation == "failed_at_head"
        else "now must be a timestamp"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"operation": "read_rows"},
        {"operation": "parse_rows", "text": 5},
        {"operation": "parse_rows", "surprise": 1},
        {"operation": "owner", "open_claims": [{"unexpected": True}]},
    ],
)
def test_error_chain_refuses_malformed_envelopes(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelLedgerTailRequest.model_validate(payload)


def test_models_are_frozen_and_result_round_trips() -> None:
    request = ModelLedgerTailRequest(operation="parse_rows")
    with pytest.raises(ValidationError):
        request.text = "changed"
    result = HANDLER.handle(request)
    assert ModelLedgerTailResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        result.refusal = "changed"
    row = ModelLedgerTailRow(ts="stamp", type="CLAIM")
    with pytest.raises(ValidationError):
        row.raw = "changed"


async def _run(tmp_path: Path, payload: dict[str, Any], name: str) -> RuntimeLocal:
    input_path = tmp_path / f"{name}.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / f"state-{name}",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "value", "refusal"),
    [
        (
            {
                "operation": "claim_names",
                "text": "repo=market pr=42",
                "pr": "omnimarket#42",
            },
            "pr-field",
            None,
        ),
        (
            {"operation": "next_drain_lane", "lane": "repo-drains-1z"},
            None,
            "'repo-drains-1z' ends in z: pass the successor lane explicitly",
        ),
    ],
)
async def test_golden_chain_over_the_bus(
    tmp_path: Path, payload: dict[str, Any], value: Any, refusal: str | None
) -> None:
    runtime = await _run(tmp_path, payload, "tail")
    result = runtime.handler_result
    assert isinstance(result, ModelLedgerTailResult)
    assert result.value == value
    assert result.refusal == refusal


@pytest.mark.asyncio
async def test_error_chain_over_the_bus(tmp_path: Path) -> None:
    input_path = tmp_path / "bad.json"
    input_path.write_text('{"operation":"other"}')
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state-bad",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await runtime.run_async() is EnumWorkflowResult.FAILED
    assert runtime.handler_result is None


def test_parse_rows_normalizes_file_newlines_without_splitting_other_whitespace() -> (
    None
):
    assert ask(
        "parse_rows",
        text="heading\r\n2026-10-20T11:00:00Z | CLAIM | lane=a\r2026-10-20T11:01:00Z | STATUS | lane=b\vnote\r\n",
    ).value == [
        {
            "ts": "2026-10-20T11:00:00Z",
            "type": "CLAIM",
            "fields": {"lane": "a"},
            "raw": "2026-10-20T11:00:00Z | CLAIM | lane=a",
            "line": 2,
        },
        {
            "ts": "2026-10-20T11:01:00Z",
            "type": "STATUS",
            "fields": {"lane": "b"},
            "raw": "2026-10-20T11:01:00Z | STATUS | lane=b\vnote",
            "line": 3,
        },
    ]


def test_parsed_rows_can_be_supplied_to_row_object_operations() -> None:
    rows = [
        {
            "ts": "2026-10-20T11:00:00Z",
            "type": "CLAIM",
            "fields": {"lane": "repo-drains-1a"},
            "raw": "raw",
            "line": 7,
        }
    ]
    assert ask("live_drains", rows=rows).value == rows
    assert ask("closed_claims", rows=rows).value == []


# The recorded case: a lane hands its PR to the landing controller with one MSG row
# (to=landing-controller | pr=<owner/repo#n> | head=<sha> | needs=land) and stops. The
# MSG names the PR in its pr= cell, and read as an open CLAIM it made the PR OWNED, so the
# controller held a PR it had been handed to land.
HANDOFF_MSG = (
    "2026-10-20T11:58:00Z | MSG | lane=lane-a | to=landing-controller"
    " | pr=OmniNode-ai/omnimarket#42 | head=0123456789abcdef | needs=land | handed off"
)


def test_owner_handoff_msg_text_is_not_ownership() -> None:
    assert ask(
        "owner",
        pr="omnimarket#42",
        now=NOW,
        open_claims=[{"lane": "lane-a", "ts": "m", "at": NOW, "text": HANDOFF_MSG}],
    ).value == {"state": "FREE", "stale": []}


@pytest.mark.parametrize("rtype", ["MSG", "msg", "STATUS", "TERMINAL"])
def test_owner_typed_non_claim_row_is_not_ownership(rtype: str) -> None:
    assert ask(
        "owner",
        pr="omnimarket#42",
        now=NOW,
        open_claims=[
            {"lane": "lane-a", "at": NOW, "type": rtype, "text": "pr=market#42"}
        ],
    ).value == {"state": "FREE", "stale": []}


def test_owner_claim_row_still_owns_beside_a_handoff_msg() -> None:
    claim = (
        "2026-10-20T11:50:00Z | CLAIM | lane=lane-b | ticket=T-1"
        " | pr=OmniNode-ai/omnimarket#42 | fixing it"
    )
    for extra in ({}, {"type": "CLAIM"}):
        assert ask(
            "owner",
            pr="omnimarket#42",
            now=NOW,
            open_claims=[
                {"lane": "lane-b", "ts": "c", "at": NOW, "text": claim, **extra},
                {"lane": "lane-a", "ts": "m", "at": NOW, "text": HANDOFF_MSG},
            ],
        ).value == {
            "state": "OWNED",
            "by": "claim",
            "lane": "lane-b",
            "claim_ts": "c",
            "why": "pr-field",
            "stale": [],
        }
