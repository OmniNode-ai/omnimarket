# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing watcher facts node: facts for a switched-over repository from the watcher's state (OMN-23507).

Golden: a watcher state holding open, merged and other-repository pull requests ends in the
written event on the written topic and facts holding exactly the open pull requests of the
overlay's repositories, a red head carrying the overlay's red class and its failed run ids.
Error: a state older than the bound, an unreadable state and an unwritable facts file each end in
the failed event on the failed topic, and nothing is written.
Failing control: the overlay refuses an empty repository list, so a node that names no repository
cannot run and write empty facts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.landing_decision import ModelLandingFacts
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingCi,
    EnumLandingMergeState,
    EnumLandingRedClass,
    EnumLandingSuspension,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.handlers.handler_pr_landing_watcher_facts import (
    HandlerPrLandingWatcherFacts,
    map_watcher_state,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.handlers.watcher_facts_ports import (
    InMemoryLandingFactsSink,
    InMemoryWatcherFactsPublisher,
    InMemoryWatcherStateSource,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.handlers.watcher_facts_sources import (
    EVENT_JOURNAL_NAME,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.models.model_watcher_facts import (
    EnumWatcherFactsFailureReason,
    EnumWatcherFactsStatus,
    ModelWatcherFactsFailed,
    ModelWatcherFactsRequest,
    ModelWatcherFactsWritten,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.models.model_watcher_facts_overlay import (
    ModelWatcherFactsOverlay,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.runtime_watcher_facts import (
    run_once,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_landing_watcher_facts_effect"
)
NOW = datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC)
STAMP = (NOW - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
FIXED_ID = UUID("00000000-0000-4000-8000-000000000003")
WRITTEN_TOPIC = "onex.evt.omnimarket.pr-landing-watcher-facts-written.v1"
FAILED_TOPIC = "onex.evt.omnimarket.pr-landing-watcher-facts-failed.v1"


def _overlay(**update: object) -> ModelWatcherFactsOverlay:
    raw = yaml.safe_load(
        (NODE / "watcher_facts.overlay.example.yaml").read_text(encoding="utf-8")
    )
    raw.update(update)
    return ModelWatcherFactsOverlay.model_validate(raw)


def _entry(
    repo: str,
    number: int,
    head: str,
    *,
    state: str = "OPEN",
    verdict: str = "GREEN",
    merge_state: str = "CLEAN",
    draft: bool = False,
    ci_sha: str | None = None,
) -> dict[str, object]:
    red = verdict == "RED"
    return {
        "facts": {
            "repo": repo,
            "number": number,
            "state": state,
            "draft": draft,
            "head_sha": head,
            "node_id": f"PR_{number}",
            "created_at": "2026-11-09T22:25:22Z",
        },
        "ci": {
            "sha": ci_sha or head,
            "verdict": verdict,
            "red": ["lint"] if red else [],
            "cancelled": [],
            "red_facts": [
                ["lint", "failure", "77", STAMP],
                ["lint", "failure", "77", STAMP],
            ]
            if red
            else [],
        },
        "review": {"merge_state": merge_state},
        "queued": False,
    }


def _state(last_tick: str = STAMP) -> dict[str, object]:
    return {
        "last_tick": last_tick,
        "prs": {
            "omnimemory#1": _entry("omnimemory", 1, "1" * 40),
            "omnimemory#2": _entry(
                "omnimemory", 2, "2" * 40, verdict="RED", merge_state="BLOCKED"
            ),
            "omnimemory#3": _entry("omnimemory", 3, "3" * 40, state="MERGED"),
            "omnimemory#4": _entry(
                "omnimemory", 4, "4" * 40, draft=True, ci_sha="5" * 40, verdict="RED"
            ),
            "omnimarket#9": _entry("omnimarket", 9, "9" * 40),
        },
    }


def _handler(
    state: object, **kw: object
) -> tuple[
    HandlerPrLandingWatcherFacts,
    InMemoryLandingFactsSink,
    InMemoryWatcherFactsPublisher,
]:
    sink, pub = InMemoryLandingFactsSink(), InMemoryWatcherFactsPublisher()
    handler = HandlerPrLandingWatcherFacts(
        overlay=_overlay(**kw),
        state_source=InMemoryWatcherStateSource(state),
        sink=sink,
        publisher=pub,
        now=lambda: NOW,
    )
    return handler, sink, pub


def test_golden_open_prs_of_the_named_repository_are_written() -> None:
    handler, sink, pub = _handler(_state())

    result = handler.handle(ModelWatcherFactsRequest(correlation_id=FIXED_ID))

    assert result.status is EnumWatcherFactsStatus.WRITTEN
    assert len(sink.written) == 1
    facts = sink.written[0]
    prs = {p.pr: p for p in facts.prs}
    assert sorted(prs) == [
        "OmniNode-ai/omnimemory#1",
        "OmniNode-ai/omnimemory#2",
        "OmniNode-ai/omnimemory#4",
    ]
    red = prs["OmniNode-ai/omnimemory#2"]
    assert red.ci is EnumLandingCi.RED
    assert red.red_class is EnumLandingRedClass.PRODUCT
    assert red.red_checks == ("lint",)
    assert red.failed_run_ids == (77,)
    assert red.merge_state is EnumLandingMergeState.BLOCKED
    assert prs["OmniNode-ai/omnimemory#1"].pr_node_id == "PR_1"
    draft = prs["OmniNode-ai/omnimemory#4"]
    assert draft.suspensions == (EnumLandingSuspension.DRAFT,)
    assert draft.ci is EnumLandingCi.PENDING  # its ci reading is at another head
    assert draft.failed_run_ids == ()
    assert facts.tick == int((NOW - timedelta(seconds=30)).timestamp())
    assert pub.published == [
        (
            WRITTEN_TOPIC,
            ModelWatcherFactsWritten(
                correlation_id=FIXED_ID,
                tick=facts.tick,
                prs=3,
                repositories=("omnimemory",),
                facts_path="memory",
            ),
        )
    ]


def test_the_same_state_gives_the_same_tick() -> None:
    assert map_watcher_state(_state(), _overlay()) == map_watcher_state(
        _state(), _overlay()
    )


def test_error_a_stale_state_ends_in_the_failed_event_and_writes_nothing() -> None:
    old = (NOW - timedelta(seconds=901)).strftime("%Y-%m-%dT%H:%M:%SZ")
    handler, sink, pub = _handler(_state(last_tick=old))

    result = handler.handle(ModelWatcherFactsRequest(correlation_id=FIXED_ID))

    assert result.status is EnumWatcherFactsStatus.FAILED
    assert result.failure_reason is EnumWatcherFactsFailureReason.STATE_STALE
    assert sink.written == []
    assert [(t, type(e)) for t, e in pub.published] == [
        (FAILED_TOPIC, ModelWatcherFactsFailed)
    ]


@pytest.mark.parametrize("state", [{}, [], {"prs": {}, "last_tick": "yesterday"}])
def test_error_an_unreadable_state_ends_in_the_failed_event(state: object) -> None:
    handler, sink, pub = _handler(state)

    result = handler.handle(ModelWatcherFactsRequest(correlation_id=FIXED_ID))

    assert result.failure_reason is EnumWatcherFactsFailureReason.STATE_UNREADABLE
    assert sink.written == []
    assert pub.published[0][0] == FAILED_TOPIC


def test_error_an_unwritable_facts_file_ends_in_the_failed_event(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state()), encoding="utf-8")
    overlay = _overlay(
        backend="file",
        watcher_state_path=str(state_path),
        facts_path=str(blocker / "last-facts.json"),
        emission_journal=str(tmp_path / "emissions.jsonl"),
    )

    code, line = run_once(overlay, now=lambda: NOW)

    assert code == 1
    assert line["failure_reason"] == "facts_unwritable"


def test_failing_control_the_overlay_refuses_an_empty_repository_list() -> None:
    with pytest.raises(ValidationError):
        _overlay(repositories=[])
    with pytest.raises(ValidationError):
        _overlay(repositories=["OmniNode-ai/omnimemory"])


def test_file_backend_writes_facts_the_landing_facts_model_reads(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state()), encoding="utf-8")
    facts_path = tmp_path / "out" / "last-facts.json"
    overlay = _overlay(
        backend="file",
        watcher_state_path=str(state_path),
        facts_path=str(facts_path),
        emission_journal=str(tmp_path / "emissions.jsonl"),
    )

    code, line = run_once(overlay, now=lambda: NOW)

    assert code == 0, line
    facts = ModelLandingFacts.model_validate_json(
        facts_path.read_text(encoding="utf-8")
    )
    assert len(facts.prs) == 3
    assert line["prs"] == 3
    events = (tmp_path / EVENT_JOURNAL_NAME).read_text(encoding="utf-8").splitlines()
    assert len(events) == 1
    assert WRITTEN_TOPIC in events[0]
    assert (tmp_path / "emissions.jsonl").exists()


def test_file_backend_needs_its_paths() -> None:
    with pytest.raises(ValidationError):
        _overlay(backend="file")


def test_contract_declares_the_overlay_topics() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    overlay = yaml.safe_load(
        (NODE / "watcher_facts.overlay.file.example.yaml").read_text(encoding="utf-8")
    )
    assert contract["event_bus"]["publish_topics"] == [
        overlay["written_topic"],
        overlay["failed_topic"],
    ]
    assert contract["runtime_dispatch"]["terminal_events"] == {
        "success": overlay["written_topic"],
        "failure": overlay["failed_topic"],
    }


ENTRY = ["-m", "omnimarket.nodes.node_pr_landing_watcher_facts_effect"]


def _runtime_pass(
    tmp_path: Path, last_tick: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state(last_tick=last_tick)), encoding="utf-8")
    facts_path = tmp_path / "out" / "last-facts.json"
    raw = yaml.safe_load(
        (NODE / "watcher_facts.overlay.file.example.yaml").read_text(encoding="utf-8")
    )
    raw.update(
        watcher_state_path=str(state_path),
        facts_path=str(facts_path),
        emission_journal=str(tmp_path / "emissions.jsonl"),
    )
    root = tmp_path / "overlay_root" / "node_pr_landing_watcher_facts_effect"
    root.mkdir(parents=True)
    (root / "overlay.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, *ENTRY],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlay_root")},
    )
    return result, facts_path


def _last_line(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_runtime_entry_prints_the_written_line_naming_omnimemory(
    tmp_path: Path,
) -> None:
    # the runtime entry reads the real clock, so its state must be fresh against it
    fresh = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result, facts_path = _runtime_pass(tmp_path, fresh)

    assert result.returncode == 0, result.stdout + result.stderr
    line = _last_line(result)
    assert line["status"] == "written"
    assert "omnimemory" in line["repositories"]
    assert line["prs"] == 3
    facts = ModelLandingFacts.model_validate_json(
        facts_path.read_text(encoding="utf-8")
    )
    assert len(facts.prs) == 3
    assert {p.pr.split("#")[0].split("/")[1] for p in facts.prs} == {"omnimemory"}


def test_runtime_entry_error_chain_stale_state_exits_one_and_writes_nothing(
    tmp_path: Path,
) -> None:
    old = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result, facts_path = _runtime_pass(tmp_path, old)

    assert result.returncode == 1, result.stdout + result.stderr
    line = _last_line(result)
    assert line["status"] != "written"
    assert "failure_reason" in line
    assert not facts_path.exists()


def test_runtime_entry_refuses_without_an_overlay(tmp_path: Path) -> None:
    empty = tmp_path / "overlay_root"
    empty.mkdir()
    result = subprocess.run(
        [sys.executable, *ENTRY],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "ONEX_SKILL_OVERLAY_ROOTS": str(empty)},
    )

    assert result.returncode == 3
    assert _last_line(result)["status"] == "overlay_refused"
