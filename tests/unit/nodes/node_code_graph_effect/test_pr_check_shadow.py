# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-22902): the check runs in shadow over the declared repositories, logs what it would refuse, holds no merge.

The shadow reads the open pull requests of the overlay's shadow repositories, checks each head
against its base, and appends one row. It decides nothing: the result never holds a merge. The
failing controls are the node's declared faults: the contract links dropped turn a wired head into
a would-refuse, and an unreadable pull request source turns the run into a typed failure and a row
that says so.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_code_graph import (
    EnumGraphFault,
    EnumShadowOutcome,
    ModelRepoSnapshot,
    ModelShadowCommand,
    ModelShadowResult,
)
from omnimarket.nodes.node_code_graph_effect.adapters.pull_request_sources import (
    InMemoryPullRequestSource,
    WatcherStatePullRequestSource,
)
from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import (
    _contract_modules,
)
from omnimarket.nodes.node_code_graph_effect.handlers.shadow_check import (
    CodeGraphShadowCheck,
)
from omnimarket.nodes.node_code_graph_effect.protocols import PullRequestSourceError
from omnimarket.nodes.node_code_graph_effect.runtime_code_graph import main
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from tests.nodes.node_code_graph_effect.code_graph_support import (
    APP,
    CORE,
    EXAMPLE,
    HOST,
    RUN_AT,
    commit,
    make_clone,
    overlay,
)
from tests.unit.nodes.node_code_graph_effect.pr_support import (
    CLEAN_SHA,
    PLANTED,
    PLANTED_SHA,
    WIRED,
    memory_source,
    pull_request,
)

pytestmark = pytest.mark.unit

MISSING_SHA = "9" * 40


def _handler(
    ledger: InMemoryLedger | None, pulls: InMemoryPullRequestSource, **changes: object
):
    return CodeGraphShadowCheck(
        overlay=overlay(**changes),
        host=HOST,
        source=memory_source({CLEAN_SHA: WIRED, PLANTED_SHA: PLANTED}),
        pull_requests=pulls,
        appender=ledger,
        clock=lambda: RUN_AT,
    )


def _pulls() -> InMemoryPullRequestSource:
    return InMemoryPullRequestSource(
        [
            pull_request(11, CLEAN_SHA),
            pull_request(12, PLANTED_SHA),
            pull_request(13, MISSING_SHA),
        ]
    )


def _command(**changes: object) -> ModelShadowCommand:
    return ModelShadowCommand.model_validate({"correlation_id": "shadow-1", **changes})


def test_the_shadow_logs_what_it_would_refuse_and_holds_nothing() -> None:
    ledger = InMemoryLedger()
    result = _handler(ledger, _pulls()).handle(_command())

    assert result.status == "completed"
    assert result.holds_merge is False
    by_number = {verdict.number: verdict for verdict in result.verdicts}
    assert by_number[11].outcome is EnumShadowOutcome.WOULD_PASS
    assert by_number[12].outcome is EnumShadowOutcome.WOULD_REFUSE
    assert by_number[12].flagged == ("src/example_app/planted.py",)
    assert by_number[13].outcome is EnumShadowOutcome.INDETERMINATE
    assert by_number[13].reason.startswith("ref-unreadable")

    assert result.ledger == "written"
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.cells["src"] == "code-graph-shadow"
    assert row.cells["gate"] == "none"
    assert row.cells["checked"] == "3"
    assert row.cells["would_refuse"] == "1"
    assert row.cells["would_pass"] == "1"
    assert row.cells["indeterminate"] == "1"
    assert row.cells["refused"] == "example_app#12:src/example_app/planted.py"
    assert row.cells["published_at"] == "2026-11-07T06:40:00Z"
    assert row.cells["max_age"] == "3600"
    assert row.cells["drill"] == "no"


def test_a_result_that_holds_a_merge_cannot_be_built() -> None:
    with pytest.raises(ValidationError):
        ModelShadowResult.model_validate(
            {"correlation_id": "x", "status": "completed", "holds_merge": True}
        )


def test_only_the_declared_repositories_are_checked() -> None:
    pulls = InMemoryPullRequestSource(
        [
            pull_request(11, CLEAN_SHA),
            pull_request(12, PLANTED_SHA).model_copy(update={"repo": "example_core"}),
        ]
    )
    result = _handler(None, pulls).handle(_command(write=False))
    assert [(v.repo, v.number) for v in result.verdicts] == [("example_app", 11)]


def test_a_run_past_its_bound_says_how_many_it_skipped() -> None:
    shadow = {
        **yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))["shadow"],
        "max_pull_requests": 1,
    }
    ledger = InMemoryLedger()
    result = _handler(ledger, _pulls(), shadow=shadow).handle(_command())
    assert [v.number for v in result.verdicts] == [13]
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.cells["skipped"] == "2"


def test_a_probe_appends_nothing() -> None:
    ledger = InMemoryLedger()
    result = _handler(ledger, _pulls()).handle(_command(write=False))
    assert result.ledger == "not-written"
    assert ledger.lines == []


def test_failing_control_dropped_contract_links_turn_a_wired_head_into_a_would_refuse() -> (
    None
):
    wired_only = InMemoryPullRequestSource([pull_request(11, CLEAN_SHA)])
    ok = _handler(None, wired_only).handle(_command(write=False))
    assert ok.count(EnumShadowOutcome.WOULD_PASS) == 1

    broken = _handler(None, wired_only).handle(
        _command(write=False, fault=EnumGraphFault.DROP_CONTRACT_LINKS)
    )
    assert broken.count(EnumShadowOutcome.WOULD_REFUSE) == 1
    assert broken.holds_merge is False


def test_failing_control_an_unreadable_source_is_a_typed_failure_and_a_row() -> None:
    ledger = InMemoryLedger()
    result = _handler(ledger, InMemoryPullRequestSource(error="state-gone")).handle(
        _command()
    )
    assert result.status == "failed"
    assert result.failure == "pull-requests-unreadable:state-gone"
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.cells["outcome"] == "source-unreadable"
    assert row.cells["checked"] == "0"


def test_a_failed_append_fails_the_run() -> None:
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    result = _handler(ledger, _pulls()).handle(_command())
    assert result.status == "failed"
    assert (result.failure or "").startswith("append-failed:refused")


def test_the_watcher_state_gives_the_open_pull_requests_of_a_repository(
    tmp_path: Path,
) -> None:
    def entry(repo: str, number: int, state: str) -> dict[str, object]:
        return {
            "facts": {
                "repo": repo,
                "number": number,
                "state": state,
                "base": "dev",
                "head_ref": f"branch-{number}",
                "head_sha": f"{number:040d}",
            }
        }

    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "prs": {
                    "example_app#5": entry("example_app", 5, "OPEN"),
                    "example_app#7": entry("example_app", 7, "OPEN"),
                    "example_app#6": entry("example_app", 6, "MERGED"),
                    "example_core#8": entry("example_core", 8, "OPEN"),
                }
            }
        ),
        encoding="utf-8",
    )
    found = WatcherStatePullRequestSource(state).open_pull_requests("example_app")
    assert [pr.number for pr in found] == [7, 5]
    assert found[0].base_ref == "origin/dev"
    assert found[0].head_sha == f"{7:040d}"

    state.write_text("not json", encoding="utf-8")
    with pytest.raises(PullRequestSourceError, match="watcher-state-unreadable"):
        WatcherStatePullRequestSource(state).open_pull_requests("example_app")
    with pytest.raises(PullRequestSourceError, match="watcher-state-unreadable"):
        WatcherStatePullRequestSource(tmp_path / "absent.json").open_pull_requests("x")


def _node_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, shadow: bool
) -> Path:
    clones = tmp_path / "clones"
    make_clone(clones, "example_core", CORE)
    make_clone(clones, "example_app", APP)
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["graph_backend"] = "git"
    if not shadow:
        del document["shadow"]
    target = tmp_path / "overlays" / "node_code_graph_effect" / "overlay.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "overlays"))
    monkeypatch.setenv("EXAMPLE_REPOS_ROOT", str(clones))
    monkeypatch.setenv("EXAMPLE_HOST_NAME", HOST)
    return clones / "example_app"


def test_the_shadow_verb_on_real_clones_exits_zero_whatever_it_would_refuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = _node_world(tmp_path, monkeypatch, shadow=True)
    planted = commit(app, PLANTED, "plant")
    state = tmp_path / "watcher.json"
    state.write_text(
        json.dumps(
            {
                "last_tick": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "prs": {
                    "example_app#21": {
                        "facts": {
                            "repo": "example_app",
                            "number": 21,
                            "state": "OPEN",
                            "base": "main",
                            "head_ref": "planted",
                            "head_sha": planted,
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EXAMPLE_PR_STATE_PATH", str(state))

    code = main(["shadow", "--probe"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "WOULD-REFUSE example_app#21 src/example_app/planted.py" in out
    assert "would_refuse=1" in out
    assert "holds_merge=no" in out

    state.unlink()
    code = main(["shadow", "--probe"])
    captured = capsys.readouterr()
    assert code == 1
    assert "pull-requests-unreadable" in captured.err


def test_the_shadow_verb_without_a_declared_shadow_is_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _node_world(tmp_path, monkeypatch, shadow=False)
    assert main(["shadow", "--probe"]) == 3


def test_a_run_reads_each_base_once_however_many_pull_requests_target_it() -> None:
    reads: list[tuple[str, str]] = []
    source = memory_source({CLEAN_SHA: WIRED, PLANTED_SHA: PLANTED})
    original = source.snapshot

    def counting(
        repo: str, ref: str, wanted: Callable[[str], bool]
    ) -> ModelRepoSnapshot:
        reads.append((repo, ref))
        return original(repo, ref, wanted)

    object.__setattr__(source, "snapshot", counting)
    handler = CodeGraphShadowCheck(
        overlay=overlay(),
        host=HOST,
        source=source,
        pull_requests=_pulls(),
        appender=None,
        clock=lambda: RUN_AT,
    )
    handler.handle(_command(write=False))
    assert reads.count(("example_app", "origin/main")) == 1
    assert len(reads) == 1 + 3  # the base once, and each of the three heads


class _ThenFails:
    """The first repository gives its pull requests; a later one is unreadable."""

    def __init__(self, first: InMemoryPullRequestSource) -> None:
        self._first = first

    def open_pull_requests(self, repo: str):
        if repo == "example_app":
            return self._first.open_pull_requests(repo)
        raise PullRequestSourceError("later-repo-gone")


def test_a_later_repository_source_error_keeps_the_verdicts_already_read() -> None:
    shadow = {
        **yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))["shadow"],
        "repos": ["example_app", "example_core"],
    }
    ledger = InMemoryLedger()
    handler = CodeGraphShadowCheck(
        overlay=overlay(shadow=shadow),
        host=HOST,
        source=memory_source({CLEAN_SHA: WIRED, PLANTED_SHA: PLANTED}),
        pull_requests=_ThenFails(_pulls()),
        appender=ledger,
        clock=lambda: RUN_AT,
    )
    result = handler.handle(_command())
    assert result.status == "failed"
    assert result.failure == "pull-requests-unreadable:later-repo-gone"
    assert [v.number for v in result.verdicts] == [13, 12, 11]
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.cells["outcome"] == "source-unreadable"
    assert row.cells["checked"] == "3"
    assert row.cells["would_refuse"] == "1"


def _watcher_state(tmp_path: Path, document: object) -> Path:
    state = tmp_path / "state.json"
    state.write_text(json.dumps(document), encoding="utf-8")
    return state


_OPEN = {
    "repo": "example_app",
    "number": 5,
    "state": "OPEN",
    "base": "dev",
    "head_ref": "b",
    "head_sha": "a" * 40,
}


def test_a_watcher_state_older_than_the_freshness_bound_is_a_source_error(
    tmp_path: Path,
) -> None:
    state = _watcher_state(
        tmp_path,
        {"last_tick": "2026-11-07T04:00:00Z", "prs": {"x#5": {"facts": _OPEN}}},
    )
    source = WatcherStatePullRequestSource(
        state, max_age=timedelta(seconds=3600), clock=lambda: RUN_AT
    )
    with pytest.raises(PullRequestSourceError, match="watcher-state-stale"):
        source.open_pull_requests("example_app")
    fresh = _watcher_state(
        tmp_path,
        {"last_tick": "2026-11-07T06:30:00Z", "prs": {"x#5": {"facts": _OPEN}}},
    )
    assert [
        pr.number
        for pr in WatcherStatePullRequestSource(
            fresh, max_age=timedelta(seconds=3600), clock=lambda: RUN_AT
        ).open_pull_requests("example_app")
    ] == [5]
    no_tick = _watcher_state(tmp_path, {"prs": {"x#5": {"facts": _OPEN}}})
    with pytest.raises(PullRequestSourceError, match="watcher-state-stale"):
        WatcherStatePullRequestSource(
            no_tick, max_age=timedelta(seconds=3600), clock=lambda: RUN_AT
        ).open_pull_requests("example_app")


def test_a_watcher_entry_with_no_facts_is_a_source_error_not_an_empty_inventory(
    tmp_path: Path,
) -> None:
    state = _watcher_state(tmp_path, {"prs": {"example_app#5": {}}})
    with pytest.raises(PullRequestSourceError, match="watcher-entry-incomplete"):
        WatcherStatePullRequestSource(state).open_pull_requests("example_app")


@pytest.mark.parametrize("number", [0, -3, True])
def test_a_watcher_pull_request_number_below_one_is_a_source_error(
    tmp_path: Path, number: object
) -> None:
    state = _watcher_state(
        tmp_path, {"prs": {"x": {"facts": {**_OPEN, "number": number}}}}
    )
    with pytest.raises(PullRequestSourceError, match="watcher-entry-incomplete"):
        WatcherStatePullRequestSource(state).open_pull_requests("example_app")


def test_a_contract_with_a_cyclic_yaml_alias_is_read_once_and_ends() -> None:
    text = "&loop\nmodule: example_app.wired\nagain: *loop\n"
    # A cycle that is walked again and again yields without end; the bound makes a broken traversal
    # fail the assertion instead of hanging the run.
    found = list(itertools.islice(_contract_modules(text, ["module"]), 50))
    assert found == ["example_app.wired"]
