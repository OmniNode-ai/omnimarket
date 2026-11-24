# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A missing repository never empties the graph: the present ones are built and the absent one is alerted once (OMN-26104).

The falsifiers: with one clone removed, the other repositories are in the stored version, the verdict says
PARTIAL, and exactly one alert names the missing repository; a second run changes nothing until the reason
changes or the repository returns and goes missing again.
"""

from __future__ import annotations

import json
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_code_graph import (
    EnumCodeGraphVerdict,
    EnumGraphFault,
    ModelCodeGraphCommand,
)
from omnimarket.models.model_code_graph_lookup import ModelCodeGraphVersion
from omnimarket.models.model_reading import (
    EnumReadingOutcome,
    EnumReadingVerdict,
    ModelPieceReadingSpec,
    ModelReadingComputeRequest,
    ModelReadingSourceRows,
)
from omnimarket.nodes.node_code_graph_effect.adapters.missing_repo_alerts import (
    ChannelMissingRepoAlerts,
    ShellAlertChannel,
)
from omnimarket.nodes.node_code_graph_effect.adapters.repo_sources import (
    InMemoryRepoSource,
)
from omnimarket.nodes.node_code_graph_effect.adapters.version_stores import (
    InMemoryVersionStore,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_run import (
    CodeGraphRun,
)
from omnimarket.nodes.node_code_graph_effect.overlay import (
    ModelCodeGraphAlert,
    ModelCodeGraphOverlay,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    HandlerReadingCompute,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from tests.nodes.node_code_graph_effect.code_graph_support import (
    APP,
    CORE,
    EXAMPLE,
    HOST,
    REF,
    RUN_AT,
    make_clone,
    overlay,
)

pytestmark = pytest.mark.unit

PIECE_1 = ModelPieceReadingSpec.model_validate(
    {
        "piece": 1,
        "name": "code graph",
        "readable_today": True,
        "basis": "nightly-graph-row",
        "sources": [
            {
                "source_id": "nightly",
                "row_type": "STATUS",
                "lane": "code-graph",
                "cells": {"src": "code-graph", "drill": "no"},
                "mode": "newest",
                "pass_when": [{"cell": "verdict", "op": "eq", "value": "WORKING"}],
                "max_age_seconds": 93600,
            }
        ],
        "injected_fault": "Stop the timer and advance the clock",
    }
)


#: What the in-memory source says for a repository it does not hold; a git source says ``no-clone:<repo>``.
MEMORY_REASON = "no-repository:example_app@origin/main"


class RecordingAlerts:
    """The alerts port: remembers what it was told and answers like the real one (once per repository and reason)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self._known: dict[str, str] = {}

    def announce(
        self, missing: Mapping[str, str], *, host: str, at: datetime
    ) -> tuple[str, ...]:
        outcomes: list[str] = []
        for repo, reason in sorted(missing.items()):
            if self._known.get(repo) == reason:
                outcomes.append(f"already-alerted:{repo}")
                continue
            self.sent.append((repo, reason))
            self._known[repo] = reason
            outcomes.append(f"alerted:{repo}")
        self._known = {r: v for r, v in self._known.items() if r in missing}
        return tuple(outcomes)


def _source(*, without: str | None = None, reason_ref: str = REF) -> InMemoryRepoSource:
    repos = {
        "example_core": {reason_ref: ("c" * 40, CORE)},
        "example_app": {reason_ref: ("a" * 40, APP)},
    }
    if without:
        del repos[without]
    return InMemoryRepoSource(repos)


def _run(
    source: InMemoryRepoSource,
    *,
    ledger: InMemoryLedger | None = None,
    alerts: RecordingAlerts | None = None,
    store: InMemoryVersionStore | None = None,
    sample_overlay: ModelCodeGraphOverlay | None = None,
    fault: EnumGraphFault = EnumGraphFault.NONE,
):  # type: ignore[no-untyped-def]
    handler = CodeGraphRun(
        overlay=sample_overlay or _overlay_without_example_app_sample(),
        host=HOST,
        source=source,
        appender=ledger,
        clock=lambda: RUN_AT,
        version_writer=store,
        missing_alerts=alerts,
    )
    return handler.handle(ModelCodeGraphCommand(correlation_id="partial", fault=fault))


def _overlay_without_example_app_sample() -> ModelCodeGraphOverlay:
    """The example overlay with a sample drawn only from example_core, so a missing example_app misses nothing in it."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    entries = [e for e in document["sample"]["entries"] if e["repo"] == "example_core"]
    controls = [
        c for c in document["sample"]["controls"] if c["repo"] == "example_core"
    ]
    from omnimarket.nodes.node_code_graph_effect.overlay import (
        ModelGraphControl,
        ModelGraphSample,
        ModelGraphSampleEntry,
    )

    frozen = ModelGraphSample.freeze(
        entries=tuple(ModelGraphSampleEntry(**e) for e in entries),
        min_agree=2,
        controls=tuple(ModelGraphControl(**c) for c in controls),
    )
    document["sample"] = frozen.model_dump(mode="json")
    document["allowlist"] = []
    document["shadow"]["repos"] = ["example_core"]
    return ModelCodeGraphOverlay.model_validate(document)


def test_with_one_clone_missing_the_others_are_stored_and_one_alert_names_it() -> None:
    ledger = InMemoryLedger()
    alerts = RecordingAlerts()
    store = InMemoryVersionStore(3)
    result = _run(
        _source(without="example_app"), ledger=ledger, alerts=alerts, store=store
    )
    assert result.status == "completed"
    assert result.outcome is EnumReadingOutcome.READ
    assert result.verdict is EnumCodeGraphVerdict.PARTIAL
    assert [repo.repo for repo in result.repos] == ["example_core"]
    assert [(m.repo, m.reason) for m in result.missing] == [
        ("example_app", MEMORY_REASON)
    ]
    assert store.newest("example_core") is not None
    assert store.newest("example_app") is None
    assert alerts.sent == [("example_app", MEMORY_REASON)]
    assert result.alerts == ("alerted:example_app",)
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.cells["verdict"] == "PARTIAL"
    assert row.cells["repos"] == "1"
    assert row.cells["missing"] == "example_app"
    assert row.cells["drill"] == "no"


def test_a_second_run_with_the_same_repository_missing_raises_no_second_alert() -> None:
    alerts = RecordingAlerts()
    source = _source(without="example_app")
    _run(source, ledger=InMemoryLedger(), alerts=alerts)
    second = _run(source, ledger=InMemoryLedger(), alerts=alerts)
    assert alerts.sent == [("example_app", MEMORY_REASON)]
    assert second.alerts == ("already-alerted:example_app",)
    assert second.verdict is EnumCodeGraphVerdict.PARTIAL


def test_a_repository_that_returns_and_goes_missing_again_is_alerted_again() -> None:
    alerts = RecordingAlerts()
    _run(_source(without="example_app"), ledger=InMemoryLedger(), alerts=alerts)
    whole = _run(_source(), ledger=InMemoryLedger(), alerts=alerts)
    assert whole.verdict is EnumCodeGraphVerdict.WORKING
    assert whole.alerts == ()
    _run(_source(without="example_app"), ledger=InMemoryLedger(), alerts=alerts)
    assert alerts.sent == [
        ("example_app", MEMORY_REASON),
        ("example_app", MEMORY_REASON),
    ]


def test_every_repository_present_is_working_and_alerts_nothing() -> None:
    alerts = RecordingAlerts()
    result = _run(_source(), ledger=InMemoryLedger(), alerts=alerts)
    assert result.verdict is EnumCodeGraphVerdict.WORKING
    assert result.missing == ()
    assert alerts.sent == []


def test_every_repository_missing_still_fails_the_run() -> None:
    ledger = InMemoryLedger()
    alerts = RecordingAlerts()
    result = _run(InMemoryRepoSource({}), ledger=ledger, alerts=alerts)
    assert result.status == "failed"
    assert result.outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert result.verdict is EnumCodeGraphVerdict.NOT_WORKING
    assert {m.repo for m in result.missing} == {"example_core", "example_app"}
    assert {repo for repo, _ in alerts.sent} == {"example_core", "example_app"}


def test_an_injected_unreadable_repository_is_a_drill_and_still_fails_the_run() -> None:
    alerts = RecordingAlerts()
    result = _run(
        _source(),
        ledger=InMemoryLedger(),
        alerts=alerts,
        fault=EnumGraphFault.UNREADABLE_REPO,
    )
    assert result.status == "failed"
    assert result.verdict is EnumCodeGraphVerdict.NOT_WORKING
    assert alerts.sent == []


def test_a_missing_repository_the_sample_names_is_not_working_and_named() -> None:
    alerts = RecordingAlerts()
    result = _run(
        _source(without="example_app"),
        ledger=InMemoryLedger(),
        alerts=alerts,
        sample_overlay=overlay(),
    )
    assert result.verdict is EnumCodeGraphVerdict.NOT_WORKING
    assert any(reason.startswith("missing:example_app") for reason in result.reasons)
    assert alerts.sent == [("example_app", MEMORY_REASON)]


def test_a_partial_row_does_not_read_as_a_working_graph() -> None:
    ledger = InMemoryLedger()
    _run(_source(without="example_app"), ledger=ledger, alerts=RecordingAlerts())
    rows = tuple(row for line in ledger.lines if (row := evidence_row_from_text(line)))
    reading = HandlerReadingCompute().handle(
        ModelReadingComputeRequest(
            spec=PIECE_1,
            window_start=RUN_AT,
            window_end=RUN_AT.replace(hour=7),
            cutoff=RUN_AT.replace(hour=8),
            order_hash="o",
            overlay_hash="h",
            revision="1",
            sources=(ModelReadingSourceRows(source_id="nightly", rows=rows),),
        )
    )
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING


def test_no_alerts_port_names_the_gap_on_the_result() -> None:
    result = _run(_source(without="example_app"), ledger=InMemoryLedger(), alerts=None)
    assert result.verdict is EnumCodeGraphVerdict.PARTIAL
    assert result.alerts == ("not-configured:example_app",)


def test_the_example_overlay_declares_the_alert_channel_and_no_hard_coded_list() -> (
    None
):
    declared = overlay()
    assert isinstance(declared.alert, ModelCodeGraphAlert)
    assert declared.alert.function == "alert_channel_send"


# --- the adapter: one alert per repository until its reason changes, through the declared library -----------------


def _library(tmp_path: Path, *, exit_code: int = 0) -> tuple[Path, Path]:
    """A shell library whose alert_channel_send appends its two arguments to a file, as the real one posts them."""
    sent = tmp_path / "sent.txt"
    lib = tmp_path / "alert-channel.sh"
    lib.write_text(
        f'alert_channel_send() {{ printf "%s|%s\\n" "$1" "$2" >> "{sent}"; return {exit_code}; }}\n',
        encoding="utf-8",
    )
    lib.chmod(lib.stat().st_mode | stat.S_IXUSR)
    return lib, sent


def _channel(tmp_path: Path, lib: Path) -> ChannelMissingRepoAlerts:
    declared = overlay().alert
    assert declared is not None
    return ChannelMissingRepoAlerts(
        send=ShellAlertChannel(declared, {declared.lib_env: str(lib)}),
        category=declared.category,
        state_path=tmp_path / "alerts-state.json",
    )


def test_the_channel_sends_one_alert_per_repository_until_the_reason_changes(
    tmp_path: Path,
) -> None:
    lib, sent = _library(tmp_path)
    channel = _channel(tmp_path, lib)
    missing = {"example_gone": "no-clone:example_gone", "other": "no-clone:other"}
    first = channel.announce(missing, host=HOST, at=RUN_AT)
    assert first == ("alerted:example_gone", "alerted:other")
    assert channel.announce(missing, host=HOST, at=RUN_AT) == (
        "already-alerted:example_gone",
        "already-alerted:other",
    )
    lines = sent.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "example_gone" in lines[0]
    assert "other" not in lines[0]
    assert "no-clone:other" in lines[1]
    changed = channel.announce(
        {**missing, "example_gone": "git-rev-parse-exit-128:example_gone"},
        host=HOST,
        at=RUN_AT,
    )
    assert changed == ("alerted:example_gone", "already-alerted:other")
    assert len(sent.read_text(encoding="utf-8").splitlines()) == 3


def test_a_send_that_fails_is_retried_on_the_next_run(tmp_path: Path) -> None:
    lib, sent = _library(tmp_path, exit_code=7)
    channel = _channel(tmp_path, lib)
    missing = {"example_gone": "no-clone:example_gone"}
    outcome = channel.announce(missing, host=HOST, at=RUN_AT)
    assert outcome == ("alert-failed:example_gone:exit-7",)
    again = channel.announce(missing, host=HOST, at=RUN_AT)
    assert again == ("alert-failed:example_gone:exit-7",)
    assert len(sent.read_text(encoding="utf-8").splitlines()) == 2


def test_an_unreadable_library_is_a_typed_failure_not_a_silence(tmp_path: Path) -> None:
    channel = _channel(tmp_path, tmp_path / "absent.sh")
    outcome = channel.announce(
        {"example_gone": "no-clone:example_gone"}, host=HOST, at=RUN_AT
    )
    assert outcome == ("alert-failed:example_gone:library-unreadable",)


def test_a_repository_that_returns_clears_its_state(tmp_path: Path) -> None:
    lib, sent = _library(tmp_path)
    channel = _channel(tmp_path, lib)
    missing = {"example_gone": "no-clone:example_gone"}
    channel.announce(missing, host=HOST, at=RUN_AT)
    assert channel.announce({}, host=HOST, at=RUN_AT) == ()
    channel.announce(missing, host=HOST, at=RUN_AT)
    assert len(sent.read_text(encoding="utf-8").splitlines()) == 2
    state = json.loads((tmp_path / "alerts-state.json").read_text(encoding="utf-8"))
    assert state == {"example_gone": "no-clone:example_gone"}


def test_real_git_clone_removed_is_partial(tmp_path: Path) -> None:
    """The same falsifier on real clones: example_app has no clone on disk."""
    from omnimarket.nodes.node_code_graph_effect.adapters.repo_sources import (
        GitRepoSource,
    )

    make_clone(tmp_path, "example_core", CORE)
    alerts = RecordingAlerts()
    store = InMemoryVersionStore(3)
    handler = CodeGraphRun(
        overlay=_overlay_without_example_app_sample(),
        host=HOST,
        source=GitRepoSource(tmp_path, max_file_bytes=2_000_000),
        appender=InMemoryLedger(),
        clock=lambda: RUN_AT,
        version_writer=store,
        missing_alerts=alerts,
    )
    result = handler.handle(ModelCodeGraphCommand(correlation_id="git"))
    assert result.verdict is EnumCodeGraphVerdict.PARTIAL
    assert alerts.sent == [("example_app", "no-clone:example_app")]
    newest: ModelCodeGraphVersion | None = store.newest("example_core")
    assert newest is not None
    assert datetime(2026, 11, 7, 6, 40, tzinfo=UTC) == RUN_AT


def test_an_unset_state_path_sends_nothing_and_names_the_gap(tmp_path: Path) -> None:
    lib, sent = _library(tmp_path)
    declared = overlay().alert
    assert declared is not None
    channel = ChannelMissingRepoAlerts(
        send=ShellAlertChannel(declared, {declared.lib_env: str(lib)}),
        category=declared.category,
        state_path=None,
    )
    outcome = channel.announce(
        {"example_gone": "no-clone:example_gone"}, host=HOST, at=RUN_AT
    )
    assert outcome == ("alert-failed:example_gone:state-path-unset",)
    assert not sent.exists()
