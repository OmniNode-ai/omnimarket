# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: every port failure is named in a typed result, never raised or read as an empty answer (OMN-20677)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.ledger_reconcile import (
    ModelAppendRowsRequest,
    ModelPlannedAppend,
    ModelReadSourcesRequest,
    ModelReadSourcesResult,
    ModelReconcileOverlay,
    ModelReconcileWanted,
    ModelVerifyEvidenceRequest,
)
from omnimarket.nodes.node_ledger_reconcile_effect.handlers import (
    HandlerAppendRows,
    HandlerReadSources,
    HandlerVerifyEvidence,
)
from omnimarket.nodes.node_ledger_reconcile_effect.protocols import ReconcilePortError

from .fakes import FakeAppender, FakeClock, FakeGit, FakeGitHub, FakeHost


def _read(
    host: FakeHost,
    live_lanes: tuple[str, ...] = (),
    live_lanes_file: str | None = None,
) -> ModelReadSourcesResult:
    return asyncio.run(
        HandlerReadSources(host=host, clock=FakeClock()).handle(
            ModelReadSourcesRequest(
                live_lanes=live_lanes, live_lanes_file=live_lanes_file
            )
        )
    )


def test_a_host_that_cannot_reconcile_says_why_and_returns_no_sources() -> None:
    result = _read(
        FakeHost(blocked="gh CLI not on PATH — live PR verification is impossible")
    )
    assert result.sources is None
    assert result.error == "gh CLI not on PATH — live PR verification is impossible"


def test_a_missing_live_ledger_is_named() -> None:
    result = _read(FakeHost(ledger_missing=True))
    assert result.sources is None
    assert result.error == "live ledger missing: /registry/ledger/ledger.md"


def test_a_missing_roster_file_is_a_hard_failure_never_an_empty_roster() -> None:
    result = _read(FakeHost(), live_lanes_file="/rosters/absent.txt")
    assert result.sources is None
    assert result.error == "--live-lanes file missing: /rosters/absent.txt"


def test_a_roster_file_and_inline_lanes_are_joined() -> None:
    result = _read(
        FakeHost(roster=frozenset({"b-lane", "c-lane"})),
        live_lanes=("a-lane", "b-lane"),
        live_lanes_file="/rosters/live.txt",
    )
    assert result.sources is not None
    assert result.sources.live_lanes == ("a-lane", "b-lane", "c-lane")


def test_a_missing_overlay_stops_the_read() -> None:
    result = _read(FakeHost(overlay=None))
    assert result.sources is None
    assert result.error == "no ledger-reconcile overlay"


def test_the_overlay_is_validated_not_defaulted() -> None:
    with pytest.raises(ValidationError):
        ModelReconcileOverlay.model_validate({"repo_aliases": {}})
    with pytest.raises(ValidationError):
        ModelReconcileOverlay.model_validate({"github_org": "X", "surprise": 1})


def test_an_unreadable_registry_root_is_named_when_verifying() -> None:
    class NoRoot(FakeHost):
        def registry_root(self) -> Path:
            raise ReconcilePortError("OMNI_HOME is not set")

    result = asyncio.run(
        HandlerVerifyEvidence(github=FakeGitHub(), git=FakeGit(), host=NoRoot()).handle(
            ModelVerifyEvidenceRequest(
                wanted=ModelReconcileWanted(), github_org="X", registry_name="r"
            )
        )
    )
    assert result.error == "registry root unreadable: OMNI_HOME is not set"
    assert result.facts.prs == ()


def test_an_unreadable_ledger_path_is_named_when_appending() -> None:
    class NoLedger(FakeHost):
        def ledger_path(self) -> Path:
            raise ReconcilePortError("ONEX_LEDGER_PATH is not set")

    appender = FakeAppender()
    result = asyncio.run(
        HandlerAppendRows(appender=appender, host=NoLedger()).handle(
            ModelAppendRowsRequest(
                rows=(ModelPlannedAppend(index=0, kind="terminal", row="r"),)
            )
        )
    )
    assert result.error == "ledger path unreadable: ONEX_LEDGER_PATH is not set"
    assert result.outcomes == ()
    assert appender.rows == []


def test_each_failed_row_is_its_own_outcome_and_the_rest_are_still_tried() -> None:
    appender = FakeAppender(error="exit 75 (contention) after 5 attempts")
    result = asyncio.run(
        HandlerAppendRows(appender=appender, host=FakeHost()).handle(
            ModelAppendRowsRequest(
                rows=(
                    ModelPlannedAppend(index=0, kind="terminal", row="a"),
                    ModelPlannedAppend(index=1, kind="release", row="b"),
                )
            )
        )
    )
    assert [o.error for o in result.outcomes] == [
        "exit 75 (contention) after 5 attempts"
    ] * 2
    assert appender.rows == ["a", "b"]


def test_requests_refuse_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ModelReadSourcesRequest.model_validate({"ledger": "/x"})
    with pytest.raises(ValidationError):
        ModelAppendRowsRequest.model_validate(
            {"rows": [{"index": 0, "kind": "bogus", "row": "r"}]}
        )
