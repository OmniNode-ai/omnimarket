# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# test-literal-ok: fixtures are the foreign local paths the scrub must reject
"""Regression shapes from the recorded scrub and publish failures."""

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.nodes.node_worktree_report_publish_effect.handlers.handler_worktree_report_publish import (
    HandlerWorktreeReportPublish,
)
from omnimarket.nodes.node_worktree_report_publish_effect.handlers.host import (
    ReportPublishHost,
    Result,
    Shell,
)
from omnimarket.nodes.node_worktree_report_publish_effect.handlers.paths import (
    portabilize,
    scrub_refusal,
)
from omnimarket.nodes.node_worktree_report_publish_effect.models import (
    ModelReportRoot,
    ModelWorktreeReportPublishRequest,
)


@pytest.mark.parametrize(
    "token",
    [
        "worktree=~/workspace/omni_worktrees/OMN-19998/reports",
        "worktree=workspace/omni_worktrees/OMN-19998/reports",
        "worktree=/Users/other/workspace/omni_worktrees/OMN-19998/reports",
        "worktree=/data/fleet/workspace/omni_worktrees/OMN-19998/reports",
    ],
)
@pytest.mark.parametrize("encoding", ["markdown", "json"])
def test_copied_claim_path_token(token: str, encoding: str) -> None:
    # A report table's open_claim evidence embeds the ledger CLAIM, including worktree=.
    evidence = f"2026-10-27T13:46:10Z / CLAIM / lane=example / {token} / scope=report"
    source = (
        json.dumps({"evidence": evidence})
        if encoding == "json"
        else f"| open_claim | {evidence} |"
    )
    roots = [
        ModelReportRoot(
            label="registry",
            registry=Path("/srv/workspace"),
            worktrees=Path("/srv/workspace/omni_worktrees"),
        )
    ]
    out = portabilize(source, roots, Path("/home/current"))
    assert "worktree=omni_worktrees/OMN-19998/reports" in out
    assert "workspace/" not in out
    assert portabilize(out, roots, Path("/home/current")) == out


HEAD = "e9e107baae" + "0" * 30


def request_at(tmp_path: Path) -> ModelWorktreeReportPublishRequest:
    reports = (
        tmp_path / "2026-10-28-worktree-prune.md",
        tmp_path / "2026-10-28-worktree-prune.json",
    )
    token = "worktree=~/workspace/omni_worktrees/OMN-19998/reports"
    reports[0].write_text(f"| open_claim | 2026-10-27T13:46:10Z / CLAIM / {token} |\n")
    reports[1].write_text(json.dumps({"evidence": token}))
    return ModelWorktreeReportPublishRequest(
        correlation_id=UUID("00000000-0000-0000-0000-000000000084"),
        date=date(2026, 10, 28),
        repository="example/reports",
        ticket="OMN-19998",
        lane="daily-prune",
        checkout=tmp_path / "reports",
        worktrees_root=tmp_path / "trees",
        state_root=tmp_path / "state",
        ledger=tmp_path / "ledger.md",
        names_file=tmp_path / "names.txt",
        alert_script=tmp_path / "alert.sh",
        reports=reports,
        roots=(
            ModelReportRoot(
                label="registry",
                registry=Path("/srv/workspace"),
                worktrees=Path("/srv/workspace/omni_worktrees"),
            ),
        ),
        home=Path("/home/current"),
        executed=True,
        summary="classified and verified",
        lab="Lab: host=fixture lane=daily-prune command=classifier observed=reports verified",
    )


class RecordingShell(Shell):
    """The production adapter's process seam, including real document file writes."""

    def __init__(
        self, request: ModelWorktreeReportPublishRequest, fail: str = ""
    ) -> None:
        super().__init__({})
        self.request = request
        self.fail = fail
        self.calls: list[list[str]] = []
        self.scrubbed: list[str] = []
        self.worktree = request.worktrees_root / request.ticket / "reports"

    def run(self, argv: Sequence[str], *, timeout: float = 600) -> Result:
        call = list(argv)
        self.calls.append(call)
        if call[0] == "git":
            args = call[3:]
            if args[0] == "symbolic-ref":
                return Result(0, "origin/main\n")
            if args[0] == "rev-parse":
                return Result(0, HEAD + "\n")
            if args[:2] == ["worktree", "add"]:
                self.worktree.mkdir(parents=True)
            if args[0] == "push" and self.fail == "push":
                return Result(1, stderr="push refused")
            return Result(0)
        if call[0] == "onex-shared-doc-scrub":
            paths = [Path(p) for p in call[4:]]
            self.scrubbed = [p.read_text() for p in paths]
            if self.fail == "scrub":
                return Result(
                    1,
                    "scrub_shared_doc: person-name scan SKIPPED for records\n"
                    + "\n".join(
                        f"{p}: local-path violation /Users/other/private/file"
                        for p in paths
                    ),
                )
            assert all("workspace/" not in text for text in self.scrubbed)
            return Result(0)
        if call[:3] == ["onex", "scm", "pr-create"]:
            if self.fail == "create":
                return Result(69, stderr="bus unavailable")
            payload: object = {
                "outcome": "completed",
                "result": {"pr_number": 742, "head_sha": HEAD},
            }
            if self.fail == "receipt":
                payload = {"outcome": "completed", "result": {"pr_number": 0}}
            if self.fail == "moved-head":
                payload = {
                    "outcome": "completed",
                    "result": {"pr_number": 742, "head_sha": "f" * 40},
                }
            return Result(0, json.dumps(payload))
        if (
            call[0] == "onex-ledger"
            and "| MSG |" in call[-1]
            and self.fail == "handoff"
        ):
            return Result(1, stderr="ledger refused")
        if call[0] == "onex-ledger" and self.fail == "friction":
            return Result(1, stderr="ledger unavailable")
        if call[0] == "bash" and self.fail == "page":
            return Result(1, stderr="alert unavailable")
        return Result(0)


def test_publish_hands_off_without_waiting_on_the_recorded_blocked_checks(
    tmp_path: Path,
) -> None:
    # The recorded 2026-10-22 publish row timed out after 1800s with BLOCKED.
    # That state is the landing controller's concern and is never read by this node.
    request = request_at(tmp_path)
    shell = RecordingShell(request)
    result = HandlerWorktreeReportPublish(ReportPublishHost(shell)).handle(request)
    assert result.outcome == "handed-off"
    assert result.pr_number == 742
    assert result.head == HEAD
    assert shell.scrubbed
    assert all("worktree=omni_worktrees/" in text for text in shell.scrubbed)
    rows = [c[-1] for c in shell.calls if c[0] == "onex-ledger"]
    assert len(rows) == 1
    assert "| MSG |" in rows[0]
    assert "| to=landing-controller |" in rows[0]
    assert f"| pr=example/reports#742 | head={HEAD} | needs=land |" in rows[0]
    assert shell.calls[-1][0] == "onex-ledger", "no calls after ownership handoff"
    assert not any(c[0] in {"gh", "env"} for c in shell.calls)
    assert not any(
        a in {"fetch", "merge", "pr-checks", "pr-view", "arm", "DELETE"}
        for c in shell.calls
        for a in c
    )
    body = (
        request.state_root
        / "worktree-report-publish"
        / str(request.correlation_id)
        / "pr-body.md"
    )
    assert request.lab in body.read_text()
    assert body.read_text().endswith("Closes OMN-19998\n")


@pytest.mark.parametrize(
    ("failure", "step"),
    [
        ("scrub", "scrub"),
        ("push", "publish"),
        ("create", "publish"),
        ("receipt", "publish"),
        ("moved-head", "publish"),
        ("handoff", "publish"),
    ],
)
def test_failed_stage_remains_failed_and_reports_friction_and_page(
    tmp_path: Path, failure: str, step: str
) -> None:
    request = request_at(tmp_path)
    shell = RecordingShell(request, failure)
    result = HandlerWorktreeReportPublish(ReportPublishHost(shell)).handle(request)
    assert result.outcome == "failed"
    assert result.step == step
    assert result.friction_recorded
    assert result.page_delivered
    friction = [
        c[-1] for c in shell.calls if c[0] == "onex-ledger" and "| FRICTION |" in c[-1]
    ]
    assert len(friction) == 1
    assert "existing=OMN-19998" in friction[0]
    assert "cost=" in friction[0]
    assert f"step '{step}' failed" in friction[0]
    assert any(c[0] == "bash" and "FAILED at" in c[-1] for c in shell.calls)
    if failure in {"scrub", "push"}:
        assert "worktree kept at" in result.detail
        assert not any(c[:3] == ["onex", "scm", "pr-create"] for c in shell.calls)
        assert not any(c[3:5] == ["worktree", "remove"] for c in shell.calls)
    if failure == "scrub":
        assert all(p.name in result.detail for p in request.reports)
        assert "person-name scan SKIPPED" not in result.detail
        assert "local-path violation" in result.detail
    if failure == "handoff":
        assert result.pr_number == 742
        assert result.head == HEAD
        assert "landing handoff refused" in result.detail


def test_unknown_paths_urls_and_sibling_roots_still_reach_the_scrub() -> None:
    roots = [
        ModelReportRoot(
            label="registry",
            registry=Path("/srv/workspace"),
            worktrees=Path("/srv/workspace/omni_worktrees"),
        )
    ]
    text = "/Users/other/private/file /srv/workspace-old/file https://example/workspace/file workspace-old/file /Users/other/workspace/private/file /etc/workspace/private/file"
    assert portabilize(text, roots, Path("/home/current")) == text


def test_multiple_and_nested_roots_stay_distinct_and_idempotent() -> None:
    roots = [
        ModelReportRoot(
            label="registry",
            registry=Path("/srv/workspace"),
            worktrees=Path("/srv/workspace/omni_worktrees"),
        ),
        ModelReportRoot(
            label="archive",
            registry=Path("/srv/workspace/archive"),
            worktrees=Path("/srv/workspace/archive/omni_worktrees"),
        ),
    ]
    text = "/srv/workspace/omni_worktrees/one /srv/workspace/archive/omni_worktrees/two ~/archive/omni_worktrees/three"
    out = portabilize(text, roots)
    assert (
        out
        == "omni_worktrees/one archive/omni_worktrees/two archive/omni_worktrees/three"
    )
    assert portabilize(out, roots) == out


def test_scrub_refusal_preserves_real_alarm() -> None:
    output = "scrub_shared_doc: person-name scan SKIPPED for records\n  harmless details\nreport.md: register rule refused\n  real alarm\n"
    assert scrub_refusal(output) == "report.md: register rule refused real alarm"


@pytest.mark.parametrize("fail_reporting", ["friction", "page"])
def test_failure_of_reporting_does_not_change_failed_outcome(
    tmp_path: Path, fail_reporting: str
) -> None:
    class BrokenPublish(ReportPublishHost):
        def commit_and_push(
            self, request: ModelWorktreeReportPublishRequest
        ) -> tuple[str, str, str]:
            from omnimarket.nodes.node_worktree_report_publish_effect.handlers.host import (
                StepError,
            )

            raise StepError("publish", "required operation refused")

    request = request_at(tmp_path)
    shell = RecordingShell(request, fail_reporting)
    result = HandlerWorktreeReportPublish(BrokenPublish(shell)).handle(request)
    assert result.outcome == "failed"
    assert result.detail == "required operation refused"
    assert result.friction_recorded is (fail_reporting != "friction")
    assert result.page_delivered is (fail_reporting != "page")


@pytest.mark.parametrize(
    "change",
    [
        {"reports": ["relative.md"]},
        {"reports": ["/tmp/a/report.md", "/tmp/b/report.md"]},
        {"reports": ["/tmp/report.py"]},
        {"checkout": "relative"},
        {"lane": "bad | injected=cell"},
        {"lab": "Lab: fake\nsecond line"},
    ],
)
def test_bad_request_is_refused_before_any_write(
    tmp_path: Path, change: dict[str, object]
) -> None:
    from pydantic import ValidationError

    data = request_at(tmp_path).model_dump()
    data.update(change)
    with pytest.raises(ValidationError):
        ModelWorktreeReportPublishRequest.model_validate(data)


@pytest.mark.parametrize("failure", ["", "push", "handoff"])
def test_node_entrypoint_publishes_with_injected_host(
    tmp_path: Path, failure: str
) -> None:
    from omnimarket.nodes.node_worktree_report_publish_effect import (
        NodeWorktreeReportPublishEffect,
    )

    request = request_at(tmp_path)
    shell = RecordingShell(request, failure)
    result = NodeWorktreeReportPublishEffect(ReportPublishHost(shell)).handle(request)
    assert result.correlation_id == request.correlation_id
    assert result.outcome == ("failed" if failure else "handed-off")
    assert result.friction_recorded is bool(failure)
    assert result.page_delivered is bool(failure)
    if not failure:
        assert result.pr_number == 742
        assert result.head == HEAD


async def test_installed_contract_routes_publish_and_failure_through_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discover the installed entrypoint and use the runtime's command adapter."""
    import asyncio

    import yaml
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
    from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    from omnimarket.nodes.node_worktree_report_publish_effect.handlers import (
        host as host_module,
    )
    from omnimarket.nodes.node_worktree_report_publish_effect.node import CONTRACT

    name = "node_worktree_report_publish_effect"
    contracts = tuple(c for c in discover_contracts().contracts if c.name == name)
    assert len(contracts) == 1
    raw = yaml.safe_load(CONTRACT.read_text())
    command_topic = raw["runtime_dispatch"]["command_topic"]
    completed_topic = raw["terminal_event"]
    assert completed_topic == "onex.evt.omnimarket.worktree-report-publish-completed.v1"
    assert raw["event_bus"]["publish_topics"] == [completed_topic]
    completed: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    bus = EventBusInmemory(environment="local", group="report-publish-wiring")
    await bus.start()

    async def collect(message: ModelEventMessage) -> None:
        await completed.put(json.loads(message.value))

    # Only the OS process boundary is replaced. Discovery, request validation,
    # handler execution, file rewriting and both event transports are real.
    request = request_at(tmp_path)
    shell = RecordingShell(request)
    monkeypatch.setattr(host_module, "Shell", lambda: shell)
    try:
        await bus.subscribe(
            completed_topic, group_id="report-results", on_message=collect
        )
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=contracts),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert {r.contract_name: r.outcome.value for r in report.results} == {
            name: "wired"
        }
        assert report.results[0].topics_subscribed == (command_topic,)
        engine.freeze()
        envelope = ModelEventEnvelope[object](
            payload=request.model_dump(mode="json"),
            correlation_id=request.correlation_id,
            event_type="omnimarket.worktree-report-publish-requested",
        )
        await bus.publish(
            command_topic, None, envelope.model_dump_json().encode(), None
        )
        event = await asyncio.wait_for(completed.get(), timeout=5)
        assert event["correlation_id"] == str(request.correlation_id)
        assert event["payload"]["outcome"] == "handed-off"
        assert event["payload"]["head"] == HEAD
        shell.fail = "push"
        # The fake git removal does not delete the directory; remove only this
        # fixture directory so the next command reaches the requested push fault.
        import shutil

        shutil.rmtree(shell.worktree)
        await bus.publish(
            command_topic, None, envelope.model_dump_json().encode(), None
        )
        event = await asyncio.wait_for(completed.get(), timeout=5)
        assert event["payload"]["outcome"] == "failed"
        assert event["payload"]["friction_recorded"] is True
    finally:
        await bus.close()
