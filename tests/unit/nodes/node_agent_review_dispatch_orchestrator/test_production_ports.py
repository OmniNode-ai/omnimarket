# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The production port bindings and the request entry of the agent review dispatch.

Each binding fails closed, named, when its environment is absent; the file store keeps
the first receipt of a key; the poster sends one ``pr_review_post`` pinned to the head
and records it; a second request for a head with a stored, posted receipt posts nothing.
The source-control bus is a fake that records each typed request; no network is reached.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
)
from omnimarket.models.model_repo_write_io import (
    EnumRepoWriteOperation,
    ModelRepoWriteRequest,
)
from omnimarket.models.scm import EnumScmNode
from omnimarket.nodes.node_agent_review_dispatch_orchestrator import (
    runtime_agent_review,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.adapters.adapter_agent_review_ports import (
    SECRET_ENV,
    FileReceiptStore,
    FileTerminalLog,
    PortNotBoundError,
    RungVoterCaller,
    ScmBus,
    ScmHeadReader,
    ScmReviewPoster,
    build_production_ports,
    host_env_value,
    parse_findings,
    signing_key,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.handlers import (
    handler_agent_review_dispatch as handler_module,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.handlers.handler_agent_review_dispatch import (
    HandlerAgentReviewDispatch,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.models import (
    EnumAgentReviewOutcome,
    EnumAgentReviewReason,
    ModelAgentReceipt,
    ModelAgentReviewConfig,
    ModelAgentReviewRequest,
    ModelAgentReviewTerminal,
    ModelAgentRosterSnapshot,
    ModelAgentVoter,
    ModelAgentVoterReview,
)
from tests.node_overlay_support import install_node_overlay

from .support import (
    HEAD,
    NODE,
    OVERLAY,
    PR,
    REPO,
    FakeClock,
    FakeHead,
    FakeLab,
    FakeSigner,
    World,
)

pytestmark = pytest.mark.unit

KEY = f"{REPO}#{PR}#{HEAD}"


def receipt(receipt_id: str) -> ModelAgentReceipt:
    return ModelAgentReceipt(
        id=receipt_id,
        repo=REPO,
        pr=PR,
        head_sha=HEAD,
        reviewer_id="test-reviewer",
        voters=(),
        roster_snapshot=ModelAgentRosterSnapshot(
            voters=("voter-one",), overlay_revision="r"
        ),
        quorum_verdict="passed",
        findings_count=0,
        issued_at=datetime(2026, 11, 3, tzinfo=UTC),
        signature="s" * 8,
    )


class FakeScmBus:
    """Records each typed request and answers with the result it was given."""

    def __init__(self, answers: dict[EnumScmNode, dict[str, Any]]) -> None:
        self.answers = answers
        self.calls: list[tuple[EnumScmNode, BaseModel]] = []

    async def call(self, node: EnumScmNode, request: BaseModel) -> dict[str, Any]:
        self.calls.append((node, request))
        return self.answers[node]


# -- durable state -------------------------------------------------------------------


def test_the_file_store_keeps_the_first_receipt_of_a_key(tmp_path: Path) -> None:
    store = FileReceiptStore(tmp_path)

    async def scenario() -> tuple[ModelAgentReceipt, ModelAgentReceipt]:
        first = await store.insert_if_absent(KEY, receipt("first"))
        second = await store.insert_if_absent(KEY, receipt("second"))
        return first, second

    first, second = asyncio.run(scenario())
    assert first.id == second.id == "first"
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert not list(tmp_path.glob(".pending-*"))


def test_concurrent_inserts_store_one_receipt(tmp_path: Path) -> None:
    store = FileReceiptStore(tmp_path)

    async def scenario() -> list[ModelAgentReceipt]:
        return list(
            await asyncio.gather(
                *(store.insert_if_absent(KEY, receipt(f"r{n}")) for n in range(8))
            )
        )

    stored = asyncio.run(scenario())
    assert len({item.id for item in stored}) == 1
    assert asyncio.run(store.get(KEY)) == stored[0]


def test_an_absent_key_reads_none(tmp_path: Path) -> None:
    assert asyncio.run(FileReceiptStore(tmp_path).get(KEY)) is None


def test_the_terminal_log_keeps_the_first_terminal(tmp_path: Path) -> None:
    log = FileTerminalLog(tmp_path)
    first = ModelAgentReviewTerminal(
        command_id="c1", outcome=EnumAgentReviewOutcome.COMPLETED, receipt_id="r"
    )
    later = ModelAgentReviewTerminal(
        command_id="c1",
        outcome=EnumAgentReviewOutcome.FAILED,
        reason=EnumAgentReviewReason.PORT_ERROR,
    )

    async def scenario() -> ModelAgentReviewTerminal | None:
        await log.record(first)
        await log.record(later)
        return await log.get("c1")

    assert asyncio.run(scenario()) == first


# -- the signing key -----------------------------------------------------------------


def test_the_key_comes_from_the_environment_first(tmp_path: Path) -> None:
    env_file = tmp_path / "host.env"
    env_file.write_text(f"{SECRET_ENV}=from-file\n", encoding="utf-8")
    environ = {SECRET_ENV: "from-env", "ONEX_HOST_ENV_FILE": str(env_file)}
    assert signing_key(environ) == "from-env"


def test_the_key_falls_back_to_the_host_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / "host.env"
    env_file.write_text(
        f"# comment\nOTHER=x\nexport {SECRET_ENV}='from-file'\n", encoding="utf-8"
    )
    assert signing_key({"ONEX_HOST_ENV_FILE": str(env_file)}) == "from-file"
    assert host_env_value(env_file, "OTHER") == "x"
    assert host_env_value(env_file, "MISSING") is None


def test_an_absent_key_refuses_by_name(tmp_path: Path) -> None:
    env_file = tmp_path / "host.env"
    env_file.write_text("OTHER=x\n", encoding="utf-8")
    with pytest.raises(PortNotBoundError) as caught:
        signing_key({"ONEX_HOST_ENV_FILE": str(env_file)})
    assert SECRET_ENV in str(caught.value)
    assert str(env_file) in str(caught.value)


# -- binding ------------------------------------------------------------------------


def bound_environ(tmp_path: Path) -> dict[str, str]:
    registry = tmp_path / "registry"
    onex = registry / "omnibase_infra" / "scripts" / "onex"
    onex.parent.mkdir(parents=True)
    onex.write_text("", encoding="utf-8")
    checkout = tmp_path / "omniintelligence"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    return {
        "ONEX_STATE_DIR": str(tmp_path / "state"),
        "OMNI_HOME": str(registry),
        "ONEX_OMNIINTELLIGENCE_CHECKOUT": str(checkout),
        SECRET_ENV: "test-key",
        "OMNI_PLUGIN_ROOT": str(tmp_path / "plugin"),
        "ONEX_HOST_ENV_FILE": str(tmp_path / "absent.env"),
    }


def config(**roster_overrides: str) -> ModelAgentReviewConfig:
    fields = dict(OVERLAY)
    if roster_overrides:
        fields["roster"] = [{**OVERLAY["roster"][0], **roster_overrides}]
    return ModelAgentReviewConfig.model_validate(fields)


def test_every_port_binds_when_its_environment_is_present(tmp_path: Path) -> None:
    ports = build_production_ports(config(), environ=bound_environ(tmp_path))
    assert isinstance(ports.store, FileReceiptStore)
    assert isinstance(ports.lab, RungVoterCaller)


@pytest.mark.parametrize(
    "absent",
    ["ONEX_STATE_DIR", "OMNI_HOME", "ONEX_OMNIINTELLIGENCE_CHECKOUT", SECRET_ENV],
)
def test_an_absent_environment_refuses_by_name(tmp_path: Path, absent: str) -> None:
    environ = bound_environ(tmp_path)
    del environ[absent]
    with pytest.raises(PortNotBoundError) as caught:
        build_production_ports(config(), environ=environ)
    assert absent in str(caught.value)


def test_a_rung_no_binding_serves_refuses_at_bind(tmp_path: Path) -> None:
    with pytest.raises(PortNotBoundError) as caught:
        build_production_ports(
            config(rung="elsewhere"),
            environ=bound_environ(tmp_path),
        )
    assert "elsewhere" in str(caught.value)


def test_a_checkout_that_is_not_one_refuses(tmp_path: Path) -> None:
    environ = bound_environ(tmp_path)
    (Path(environ["ONEX_OMNIINTELLIGENCE_CHECKOUT"]) / "pyproject.toml").unlink()
    with pytest.raises(PortNotBoundError):
        build_production_ports(config(), environ=environ)


@pytest.mark.parametrize(
    "scm_bus", [{"kind": "kafka"}, {"kind": "inmemory", "lane": "dev"}, {}]
)
def test_the_overlay_names_a_kafka_lane_and_no_inmemory_lane(
    scm_bus: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        ModelAgentReviewConfig.model_validate({**OVERLAY, "scm_bus": scm_bus})


def test_the_overlay_scm_bus_reaches_the_bus(tmp_path: Path) -> None:
    fields = {**OVERLAY, "scm_bus": {"kind": "kafka", "lane": "lane-x"}}
    ports = build_production_ports(
        ModelAgentReviewConfig.model_validate(fields), environ=bound_environ(tmp_path)
    )
    bus = ports.head._bus
    assert isinstance(bus, ScmBus)
    assert (bus._kind, bus._lane) == ("kafka", "lane-x")


# -- source control ------------------------------------------------------------------


def test_the_head_reader_asks_the_gateway_for_the_pr_text() -> None:
    bus = FakeScmBus({EnumScmNode.GATEWAY: {"head_sha": HEAD}})
    assert asyncio.run(ScmHeadReader(bus).read_head(REPO, PR)) == HEAD
    node, request = bus.calls[0]
    assert node is EnumScmNode.GATEWAY
    assert isinstance(request, ModelGithubGatewayRequest)
    assert request.operation is EnumGithubGatewayOperation.PR_TEXT
    assert request.pr_number == PR


def test_a_gateway_answer_with_no_head_raises() -> None:
    bus = FakeScmBus({EnumScmNode.GATEWAY: {}})
    with pytest.raises(RuntimeError):
        asyncio.run(ScmHeadReader(bus).read_head(REPO, PR))


def test_the_poster_sends_one_pinned_review_and_records_it(tmp_path: Path) -> None:
    bus = FakeScmBus({EnumScmNode.WRITE: {"review_id": 41, "html_url": "u"}})
    poster = ScmReviewPoster(bus, record_root=tmp_path, requested_by="test-reviewer")
    stored = receipt("rid-1")

    async def scenario() -> tuple[bool, bool]:
        before = await poster.find_posted(REPO, PR, "publisher", stored.id)
        await poster.post(REPO, PR, HEAD, stored, "test-fence")
        return before, await poster.find_posted(REPO, PR, "publisher", stored.id)

    assert asyncio.run(scenario()) == (False, True)
    node, request = bus.calls[0]
    assert node is EnumScmNode.WRITE
    assert isinstance(request, ModelRepoWriteRequest)
    assert request.operation is EnumRepoWriteOperation.PR_REVIEW_POST
    assert request.expected_head_sha == HEAD
    assert request.idempotency_key == "agent-review-rid-1"
    body = request.body or ""
    block = body.split("```test-fence\n", 1)[1].split("\n```", 1)[0]
    assert ModelAgentReceipt.model_validate_json(block) == stored


def test_a_write_with_no_review_id_raises_and_records_nothing(tmp_path: Path) -> None:
    bus = FakeScmBus({EnumScmNode.WRITE: {}})
    poster = ScmReviewPoster(bus, record_root=tmp_path, requested_by="r")
    with pytest.raises(RuntimeError):
        asyncio.run(poster.post(REPO, PR, HEAD, receipt("x"), "f"))
    assert not asyncio.run(poster.find_posted(REPO, PR, "p", "x"))


def test_a_second_request_for_a_posted_head_posts_nothing(tmp_path: Path) -> None:
    """The production store, terminal log and poster: two requests, one review."""
    world = World()
    bus = FakeScmBus({EnumScmNode.WRITE: {"review_id": 7}})
    handler = HandlerAgentReviewDispatch(
        config=config(),
        head=FakeHead(world),
        lab=FakeLab(world),
        signer=FakeSigner(world),
        store=FileReceiptStore(tmp_path / "receipts"),
        poster=ScmReviewPoster(bus, record_root=tmp_path / "posted", requested_by="r"),
        terminal_log=FileTerminalLog(tmp_path / "terminals"),
        clock=FakeClock(world),
    )

    def run(command_id: str) -> ModelAgentReviewTerminal:
        return asyncio.run(
            handler.handle(
                ModelAgentReviewRequest(
                    command_id=command_id, repo=REPO, pr=PR, head_sha=HEAD
                )
            )
        )

    first, second = run("c1"), run("c2")
    assert first.outcome is second.outcome is EnumAgentReviewOutcome.COMPLETED
    assert first.receipt_id == second.receipt_id
    assert len(bus.calls) == 1
    assert len(world.lab_calls) == len(config().roster)


# -- voters --------------------------------------------------------------------------


def test_findings_parse_from_a_json_answer() -> None:
    found = parse_findings(
        'Here: {"findings": [{"path": "a.py", "line": 3, "severity": "error", '
        '"claim": "x"}]}'
    )
    assert [(f.path, f.line, f.severity) for f in found] == [("a.py", 3, "error")]
    assert parse_findings('{"findings": []}') == ()


@pytest.mark.parametrize(
    "answer", ["no json", '{"verdict": "ok"}', '{"findings": [{"path": ""}]}']
)
def test_an_answer_without_findings_is_no_vote(answer: str) -> None:
    with pytest.raises(ValueError, match=r"answer|finding"):
        parse_findings(answer)


def test_the_rung_caller_routes_each_voter_by_rung() -> None:
    seen: list[str] = []

    class Rung:
        def __init__(self, name: str) -> None:
            self.name = name

        async def review(
            self, voter: ModelAgentVoter, repo: str, pr: int, head_sha: str
        ) -> ModelAgentVoterReview:
            seen.append(self.name)
            return ModelAgentVoterReview(
                identity=voter.identity, endpoint=voter.endpoint, model=voter.model
            )

    caller = RungVoterCaller({"lab": Rung("lab"), "codex": Rung("codex")})
    for rung in ("codex", "lab"):
        asyncio.run(
            caller.review(ModelAgentVoter(identity="v", rung=rung), REPO, PR, HEAD)
        )
    assert seen == ["codex", "lab"]
    with pytest.raises(PortNotBoundError):
        asyncio.run(
            caller.review(ModelAgentVoter(identity="v", rung="other"), REPO, PR, HEAD)
        )


# -- the runtime's construction --------------------------------------------------------


def test_the_runtime_builds_the_handler_with_no_arguments_and_binds_nothing() -> None:
    """The runtime constructs from the contract on a host with no overlay or key."""
    HandlerAgentReviewDispatch(environ={})


def test_a_request_on_a_host_without_the_overlay_fails_not_bound() -> None:
    handler = HandlerAgentReviewDispatch(environ={})
    terminal = asyncio.run(
        handler.handle(
            ModelAgentReviewRequest(command_id="c1", repo=REPO, pr=PR, head_sha=HEAD)
        )
    )
    assert terminal.outcome is EnumAgentReviewOutcome.FAILED
    assert terminal.reason is EnumAgentReviewReason.NOT_BOUND


def test_a_request_on_a_host_without_a_port_fails_not_bound_and_rebinds_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(monkeypatch, tmp_path / "overlays", NODE, OVERLAY)
    environ = bound_environ(tmp_path)
    environ["ONEX_SKILL_OVERLAY_ROOTS"] = str(tmp_path / "overlays")
    state = environ.pop("ONEX_STATE_DIR")
    handler = HandlerAgentReviewDispatch(environ=environ)
    request = ModelAgentReviewRequest(command_id="c1", repo=REPO, pr=PR, head_sha=HEAD)
    assert (
        asyncio.run(handler.handle(request)).reason is EnumAgentReviewReason.NOT_BOUND
    )
    environ["ONEX_STATE_DIR"] = state
    config, ports = handler.bind()
    assert config.scm_bus.kind == "inmemory"
    assert isinstance(ports.store, FileReceiptStore)
    assert isinstance(ports.terminal_log, FileTerminalLog)


def test_an_unexpected_bind_failure_also_ends_not_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bind error of any type fails closed; it never escapes into the runtime."""
    install_node_overlay(monkeypatch, tmp_path / "overlays", NODE, OVERLAY)
    environ = bound_environ(tmp_path)
    environ["ONEX_SKILL_OVERLAY_ROOTS"] = str(tmp_path / "overlays")

    def broken(*_args: object, **_kwargs: object) -> object:
        raise ValueError("badly formed store identity")

    monkeypatch.setattr(handler_module, "build_production_ports", broken)
    handler = HandlerAgentReviewDispatch(environ=environ)
    request = ModelAgentReviewRequest(command_id="c1", repo=REPO, pr=PR, head_sha=HEAD)
    terminal = asyncio.run(handler.handle(request))
    assert terminal.outcome is EnumAgentReviewOutcome.FAILED
    assert terminal.reason is EnumAgentReviewReason.NOT_BOUND


def test_a_partial_set_of_ports_is_refused_at_construction() -> None:
    world = World()
    with pytest.raises(TypeError, match="seven ports"):
        HandlerAgentReviewDispatch(head=FakeHead(world), lab=FakeLab(world))


# -- the request entry ---------------------------------------------------------------


def test_the_entry_names_an_unbound_port_and_calls_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install_node_overlay(monkeypatch, tmp_path / "overlays", NODE, OVERLAY)
    environ = bound_environ(tmp_path)
    del environ["ONEX_STATE_DIR"]
    environ["ONEX_SKILL_OVERLAY_ROOTS"] = str(tmp_path / "overlays")
    code = runtime_agent_review.main(
        ["--repo", REPO, "--pr", str(PR), "--head-sha", HEAD],
        environ=environ,
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "ONEX_STATE_DIR" in captured.err
    assert captured.out == ""


def test_the_entry_refuses_a_malformed_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(monkeypatch, tmp_path / "overlays", NODE, OVERLAY)
    with pytest.raises(SystemExit) as caught:
        runtime_agent_review.main(
            [
                "--repo",
                REPO,
                "--pr",
                str(PR),
                "--head-sha",
                "nope",
            ],
            environ=bound_environ(tmp_path),
        )
    assert caught.value.code != 0
