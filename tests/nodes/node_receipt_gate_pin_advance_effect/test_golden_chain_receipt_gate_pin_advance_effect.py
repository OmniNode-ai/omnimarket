# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_receipt_gate_pin_advance_effect [OMN-20134].

The effect opens one pull request per stale caller, with the pin line and its
test constant in one commit, through the write node's pr_create. The ports are
fakes here except the git tag source (a real repository with real tags) and the
REST repository (a recording transport).
"""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)
from pydantic import BaseModel, JsonValue

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.models.receipt_gate_pin_friction import (
    EnumPinKind,
    EnumPinOutcomeKind,
    EnumPinRefusalClass,
    ModelPinOutcomeEvent,
)
from omnimarket.nodes.node_github_repo_write_effect.models.model_repo_write_io import (
    EnumRepoWriteOperation,
    ModelRepoWriteRequest,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_compute.handlers.handler_receipt_gate_pin_advance import (
    HandlerReceiptGatePinAdvance,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_compute.models import (
    EnumCallerVerdict,
    ModelCallerPlan,
    ModelCallerSnapshot,
    ModelCoreDevTag,
    ModelFileEdit,
    ModelPinCaller,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_effect.adapters.git_core_tag_source import (
    GitCloneCoreTagSource,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_effect.adapters.github_pin_repository import (
    GithubPinRepository,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_effect.adapters.scm_bus_pr_opener import (
    ScmWritePrOpener,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_effect.handlers.handler_receipt_gate_pin_advance_effect import (
    HandlerReceiptGatePinAdvanceEffect,
    load_effect_config,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_effect.models import (
    EnumPinAdvanceDisposition,
    EnumPinAdvanceMode,
    EnumPinPrOutcome,
    ModelDevMergeTagEvent,
    ModelReceiptGatePinAdvanceCompleted,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_effect.policy_source import (
    load_pin_policy,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_effect.protocols import (
    PinAdvanceError,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_receipt_gate_pin_advance_effect"
)
GATE = "OmniNode-ai/omnibase_core/.github/workflows/receipt-gate.yml"
WORKFLOW = ".github/workflows/call-repo-evidence-gate.yml"
HEAD = "c" * 40
OLD = ModelCoreDevTag(
    tag="dev-20261022114039-578228977721",
    commit_sha="5" * 40,
    receipt_gate_blob="a" * 40,
)
NEW = ModelCoreDevTag(
    tag="dev-20261030152335-66d35d2283c3",
    commit_sha="66d35d2283c3011c436799eacb9bce1a434ee6c9",
    receipt_gate_blob="d" * 40,
)
SOURCE = "OmniNode-ai/omnibase_core"


def _event(**fields: object) -> ModelDevMergeTagEvent:
    base: dict[str, object] = {"repo": SOURCE, "tag": NEW.tag, "sha": NEW.commit_sha}
    return ModelDevMergeTagEvent.model_validate({**base, **fields})


class FakeTags:
    def __init__(self, tags: tuple[ModelCoreDevTag, ...]) -> None:
        self.tags = tags
        self.required: list[str] = []

    def dev_tags(self, *, required_tag: str) -> tuple[ModelCoreDevTag, ...]:
        self.required.append(required_tag)
        if required_tag not in {t.tag for t in self.tags}:
            raise PinAdvanceError(f"{required_tag} is not a dev tag")
        return self.tags


class FakeRepository:
    """Every caller pinned at ``pinned``; records what was published."""

    def __init__(self, pinned: ModelCoreDevTag) -> None:
        self.pinned = pinned
        self.published: list[tuple[str, str, str, tuple[ModelFileEdit, ...]]] = []
        self.existing_branches: dict[str, str] = {}

    def read_snapshot(self, caller: ModelPinCaller) -> ModelCallerSnapshot:
        files: dict[str, str] = {}
        for site in (*caller.pin_sites, *caller.test_constants):
            ref = (
                self.pinned.tag
                if site.ref_form.value == "tag"
                else self.pinned.commit_sha
            )
            if site.path == WORKFLOW:
                files[site.path] = (
                    f"uses: {GATE}@{ref} # {self.pinned.tag}\n"
                    if site.ref_form.value == "sha"
                    else f"uses: {GATE}@{ref}\n"
                )
            elif site.path.endswith(".json"):
                files[site.path] = f'"{GATE}", "{self.pinned.tag}"]\n'
            elif site.path.endswith("required-checks.yaml"):
                files[site.path] = (
                    f'cross_repo_ref: "{GATE}@{ref}" # {self.pinned.tag}\n'
                )
            else:
                line = f'RECEIPT_GATE_PIN_{"TAG" if site.ref_form.value == "tag" else "SHA"} = "{ref}"\n'
                files[site.path] = files.get(site.path, "") + line
        return ModelCallerSnapshot(
            repository=caller.repository, base_head_sha=HEAD, files=files
        )

    def publish_branch(
        self,
        *,
        repository: str,
        base_head_sha: str,
        branch: str,
        message: str,
        edits: tuple[ModelFileEdit, ...],
    ) -> str:
        key = f"{repository}:{branch}"
        if key in self.existing_branches:
            return self.existing_branches[key]
        self.published.append((repository, base_head_sha, branch, edits))
        head = f"{len(self.published):040x}"
        self.existing_branches[key] = head
        return head


class FakeOpener:
    def __init__(self, fail_for: str | None = None) -> None:
        self.opened: list[tuple[ModelCallerPlan, str]] = []
        self.fail_for = fail_for
        self.keys: set[str] = set()

    def open(
        self,
        plan: ModelCallerPlan,
        *,
        branch_head_sha: str,
        requested_by_lane: str,
        correlation_id: UUID,
    ) -> tuple[bool, int | None, str | None]:
        if plan.repository == self.fail_for:
            raise PinAdvanceError("pr_create was refused: quota_floor")
        replayed = plan.idempotency_key in self.keys
        self.keys.add(plan.idempotency_key)
        self.opened.append((plan, branch_head_sha))
        return (
            replayed,
            100 + len(self.opened),
            f"https://example.test/{plan.repository}/pull/1",
        )


def _handler(
    pinned: ModelCoreDevTag = OLD,
    *,
    mode: EnumPinAdvanceMode = EnumPinAdvanceMode.ENFORCE,
    opener: FakeOpener | None = None,
    clock: Callable[[], datetime] | None = None,
) -> tuple[HandlerReceiptGatePinAdvanceEffect, FakeRepository, FakeOpener, FakeTags]:
    planner = HandlerReceiptGatePinAdvance()
    repository = FakeRepository(pinned)
    opener = opener or FakeOpener()
    tags = FakeTags((OLD, NEW))
    config = load_effect_config().model_copy(update={"mode": mode})
    handler = HandlerReceiptGatePinAdvanceEffect(
        planner=planner,
        config=config,
        tag_source=tags,
        repository=repository,
        pr_opener=opener,
        clock=clock,
    )
    return handler, repository, opener, tags


def test_contract_wires_the_dev_merge_tag_event_to_one_completed_event() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["node_type"] == "EFFECT_GENERIC"
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.evt.github.dev-merge-tag.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.receipt-gate-pin-advance-completed.v1",
        "onex.evt.omnimarket.receipt-gate-pin-outcome.v1",
    ]
    assert {e["event_type"]: e["topic"] for e in contract["published_events"]} == {
        "ReceiptGatePinAdvanceCompleted": "onex.evt.omnimarket.receipt-gate-pin-advance-completed.v1",
        "PinOutcomeEvent": "onex.evt.omnimarket.receipt-gate-pin-outcome.v1",
    }
    assert (
        contract["externally_produced_topics"][0]["topic"]
        == "onex.evt.github.dev-merge-tag.v1"
    )
    assert "GITHUB_TOKEN" in contract["secrets"]
    assert load_effect_config().mode is EnumPinAdvanceMode.ENFORCE


def test_a_stale_pin_opens_one_pull_request_per_caller_each_with_one_commit() -> None:
    handler, repository, opener, tags = _handler()
    done = asyncio.run(handler.advance(_event()))
    assert tags.required == [NEW.tag]
    assert done.disposition is EnumPinAdvanceDisposition.ADVANCED
    assert done.newest_tag == NEW.tag
    assert {r.outcome for r in done.results} == {EnumPinPrOutcome.OPENED}
    assert len(done.results) == len(handler._policy.callers) == 6
    assert len(repository.published) == len(opener.opened) == 6
    by_repo = {repo: edits for repo, _, _, edits in repository.published}
    market = {e.path: e.new_text for e in by_repo["OmniNode-ai/omnimarket"]}
    assert f"{GATE}@{NEW.tag}" in market[WORKFLOW]
    assert (
        f'RECEIPT_GATE_PIN_TAG = "{NEW.tag}"' in market["tests/ci/receipt_gate_pin.py"]
    )
    infra = {e.path: e.new_text for e in by_repo["OmniNode-ai/omnibase_infra"]}
    assert (
        f'RECEIPT_GATE_PIN_SHA = "{NEW.commit_sha}"'
        in infra["tests/ci/receipt_gate_pin.py"]
    )
    assert all(
        plan.branch == f"pin-advance/receipt-gate/{NEW.tag}"
        for plan, _ in opener.opened
    )


def test_a_redelivered_tag_opens_nothing_twice() -> None:
    handler, repository, _, _ = _handler()
    asyncio.run(handler.advance(_event()))
    again = asyncio.run(handler.advance(_event()))
    assert len(repository.published) == 6
    assert {r.outcome for r in again.results} == {EnumPinPrOutcome.REPLAYED}


def test_callers_already_on_the_newest_gate_get_nothing() -> None:
    handler, repository, opener, _ = _handler(NEW)
    done = asyncio.run(handler.advance(_event()))
    assert done.disposition is EnumPinAdvanceDisposition.NOTHING_STALE
    assert done.results == ()
    assert repository.published == []
    assert opener.opened == []


def test_dry_run_plans_and_reports_without_writing() -> None:
    for handler, event in (
        (_handler(mode=EnumPinAdvanceMode.DRY_RUN)[0], _event()),
        (_handler()[0], _event(dry_run=True)),
    ):
        done = asyncio.run(handler.advance(event))
        assert {r.outcome for r in done.results} == {EnumPinPrOutcome.DRY_RUN}
        assert len(done.results) == 6
    _, repository, opener, _ = _handler(mode=EnumPinAdvanceMode.DRY_RUN)
    assert repository.published == []
    assert opener.opened == []


def test_a_tag_of_another_repository_is_ignored_before_any_read() -> None:
    handler, repository, opener, tags = _handler()
    done = asyncio.run(handler.advance(_event(repo="OmniNode-ai/omnimarket")))
    assert done.disposition is EnumPinAdvanceDisposition.IGNORED
    assert tags.required == []
    assert repository.published == []
    assert opener.opened == []


def test_one_refused_pull_request_does_not_stop_the_others() -> None:
    handler, _, opener, _ = _handler(
        opener=FakeOpener(fail_for="OmniNode-ai/omniclaude")
    )
    done = asyncio.run(handler.advance(_event()))
    assert [r.repository for r in done.failed] == ["OmniNode-ai/omniclaude"]
    assert "quota_floor" in done.failed[0].detail
    assert len(opener.opened) == 5
    assert {r.outcome for r in done.results} == {
        EnumPinPrOutcome.OPENED,
        EnumPinPrOutcome.FAILED,
    }


def test_a_trigger_tag_the_clone_does_not_hold_raises_instead_of_advancing() -> None:
    handler, repository, _, _ = _handler()
    with pytest.raises(PinAdvanceError, match="not a dev tag"):
        asyncio.run(handler.advance(_event(tag="dev-20261031000000-aaaaaaaaaaaa")))
    assert repository.published == []


def test_the_planned_unjudged_callers_are_reported_not_advanced() -> None:
    class EmptySpi(FakeRepository):
        def read_snapshot(self, caller: ModelPinCaller) -> ModelCallerSnapshot:
            snapshot = super().read_snapshot(caller)
            if caller.repository.endswith("omnibase_spi"):
                return snapshot.model_copy(update={"files": {}})
            return snapshot

    handler, _, _, _ = _handler()
    handler._repository = EmptySpi(OLD)
    done = asyncio.run(handler.advance(_event()))
    assert [(u.repository, u.verdict) for u in done.unjudged] == [
        ("OmniNode-ai/omnibase_spi", EnumCallerVerdict.PIN_SITE_UNMATCHED)
    ]
    assert len(done.results) == 5


# --- git tag source: a real repository with real tags ------------------------


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


def test_git_tag_source_reads_dev_tags_with_the_gate_blob_at_each(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "core"
    gate = repo / ".github" / "workflows" / "receipt-gate.yml"
    gate.parent.mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    gate.write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    _git(repo, "tag", "dev-20261001000000-aaaaaaaaaaaa")
    (repo / "other.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")
    _git(repo, "tag", "dev-20261002000000-bbbbbbbbbbbb")
    gate.write_text("v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "three")
    _git(repo, "tag", "-a", "-m", "annotated", "dev-20261003000000-cccccccccccc")
    _git(repo, "tag", "v1.0.0")
    source = GitCloneCoreTagSource(
        repo, ".github/workflows/receipt-gate.yml", fetch_tags=False
    )
    tags = {
        t.tag: t
        for t in source.dev_tags(required_tag="dev-20261003000000-cccccccccccc")
    }
    assert set(tags) == {
        "dev-20261001000000-aaaaaaaaaaaa",
        "dev-20261002000000-bbbbbbbbbbbb",
        "dev-20261003000000-cccccccccccc",
    }
    assert tags["dev-20261001000000-aaaaaaaaaaaa"].receipt_gate_blob == (
        tags["dev-20261002000000-bbbbbbbbbbbb"].receipt_gate_blob
    )
    assert tags["dev-20261003000000-cccccccccccc"].receipt_gate_blob != (
        tags["dev-20261002000000-bbbbbbbbbbbb"].receipt_gate_blob
    )
    assert tags["dev-20261003000000-cccccccccccc"].commit_sha == _git(
        repo, "rev-parse", "HEAD"
    )
    with pytest.raises(PinAdvanceError, match="not a dev tag"):
        source.dev_tags(required_tag="dev-20261009000000-dddddddddddd")


# --- REST repository: a recording transport ----------------------------------


class RecordingTransport:
    def __init__(self, answers: dict[tuple[str, str], ModelGithubHttpResponse]) -> None:
        self.answers = answers
        self.sent: list[ModelGithubHttpRequest] = []

    def send_sync(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.sent.append(request)
        key = (request.method, request.path.split("?")[0])
        return self.answers.get(
            key, ModelGithubHttpResponse(status=404, headers={}, body={"message": "no"})
        )


def _ok(body: dict[str, object], status: int = 200) -> ModelGithubHttpResponse:
    return ModelGithubHttpResponse(status=status, headers={}, body=body)


def test_rest_repository_commits_every_edit_in_one_tree_and_one_commit() -> None:
    repo = "OmniNode-ai/omnimarket"
    transport = RecordingTransport(
        {
            ("GET", f"/repos/{repo}/git/ref/heads/pin-advance/receipt-gate/t"): _ok(
                {"message": "x"}, 404
            ),
            ("GET", f"/repos/{repo}/git/commits/{HEAD}"): _ok(
                {"tree": {"sha": "7" * 40}}
            ),
            ("POST", f"/repos/{repo}/git/trees"): _ok({"sha": "8" * 40}, 201),
            ("POST", f"/repos/{repo}/git/commits"): _ok({"sha": "9" * 40}, 201),
            ("POST", f"/repos/{repo}/git/refs"): _ok({"ref": "x"}, 201),
        }
    )
    head = GithubPinRepository(transport).publish_branch(
        repository=repo,
        base_head_sha=HEAD,
        branch="pin-advance/receipt-gate/t",
        message="m",
        edits=(
            ModelFileEdit(path="a.yml", new_text="A\n"),
            ModelFileEdit(path="b.py", new_text="B\n"),
        ),
    )
    assert head == "9" * 40
    bodies = {(r.method, r.path.rsplit("/", 1)[-1]): r.body for r in transport.sent}
    tree = bodies[("POST", "trees")]
    assert tree is not None
    assert tree["base_tree"] == "7" * 40
    entries = tree["tree"]
    assert isinstance(entries, list)
    assert [e["path"] for e in entries] == ["a.yml", "b.py"]
    commit = bodies[("POST", "commits")]
    assert commit == {"message": "m", "tree": "8" * 40, "parents": [HEAD]}
    assert bodies[("POST", "refs")] == {
        "ref": "refs/heads/pin-advance/receipt-gate/t",
        "sha": "9" * 40,
    }
    assert [r.method for r in transport.sent].count("POST") == 3


def test_rest_repository_reuses_an_existing_branch_without_writing() -> None:
    repo = "OmniNode-ai/omnimarket"
    transport = RecordingTransport(
        {("GET", f"/repos/{repo}/git/ref/heads/b"): _ok({"object": {"sha": "e" * 40}})}
    )
    head = GithubPinRepository(transport).publish_branch(
        repository=repo, base_head_sha=HEAD, branch="b", message="m", edits=()
    )
    assert head == "e" * 40
    assert [r.method for r in transport.sent] == ["GET"]


def test_rest_repository_reads_the_files_at_the_base_head_and_skips_a_missing_one() -> (
    None
):
    caller = load_pin_policy().callers[1]
    encoded = base64.b64encode(b"pin text\n").decode()
    workflow_path = caller.pin_sites[0].path
    transport = RecordingTransport(
        {
            (
                "GET",
                f"/repos/{caller.repository}/git/ref/heads/{caller.base_branch}",
            ): _ok({"object": {"sha": HEAD}}),
            (
                "GET",
                f"/repos/{caller.repository}/contents/{workflow_path}",
            ): _ok({"encoding": "base64", "content": encoded}),
        }
    )
    snapshot = GithubPinRepository(transport).read_snapshot(caller)
    assert snapshot.base_head_sha == HEAD
    assert snapshot.files[workflow_path] == "pin text\n"
    assert all(p in snapshot.files for p in [workflow_path])
    assert (
        len(snapshot.files) == 1
    )  # the missing test-constant file is absent, not an error
    contents = [r for r in transport.sent if "/contents/" in r.path]
    assert all(r.path.endswith(f"?ref={HEAD}") for r in contents)


# --- the write node's pr_create ----------------------------------------------


def test_the_opener_sends_the_write_nodes_pr_create_command() -> None:
    seen: list[ModelRepoWriteRequest] = []

    def call(request: ModelRepoWriteRequest) -> dict[str, JsonValue]:
        seen.append(request)
        return {
            "pr_number": 4321,
            "html_url": "https://example.test/pull/4321",
            "replayed": True,
        }

    handler, repository, _, _ = _handler()
    snapshot = repository.read_snapshot(handler._policy.callers[2])
    from omnimarket.nodes.node_receipt_gate_pin_advance_compute.models import (
        ModelReceiptGatePinAdvanceRequest,
    )

    plan = handler._planner.handle(
        ModelReceiptGatePinAdvanceRequest(
            policy=handler._policy,
            core_tags=(OLD, NEW),
            callers=tuple(repository.read_snapshot(c) for c in handler._policy.callers),
        )
    ).plans[2]
    assert snapshot.repository == plan.repository
    replayed, number, url = ScmWritePrOpener(call).open(
        plan,
        branch_head_sha="e" * 40,
        requested_by_lane="lane-x",
        correlation_id=uuid4(),
    )
    assert (replayed, number, url) == (True, 4321, "https://example.test/pull/4321")
    request = seen[0]
    assert request.operation is EnumRepoWriteOperation.PR_CREATE
    assert request.head == plan.branch
    assert request.base == plan.base_branch
    assert request.expected_head_sha == "e" * 40
    assert request.idempotency_key == plan.idempotency_key
    assert request.requested_by_lane == "lane-x"


# --- the friction measurement: one typed outcome event per caller outcome ------

TAG_PUSHED = datetime(2026, 10, 31, 1, 0, 0, tzinfo=UTC)
OPENED_AT = TAG_PUSHED + timedelta(seconds=90)


def _outcomes(
    handler: HandlerReceiptGatePinAdvanceEffect, event: ModelDevMergeTagEvent
) -> tuple[BaseModel, tuple[ModelPinOutcomeEvent, ...]]:
    emitted = asyncio.run(handler.handle(event))
    outcomes = tuple(e for e in emitted if isinstance(e, ModelPinOutcomeEvent))
    return emitted[0], outcomes


def test_every_opened_pull_request_emits_one_outcome_with_its_pin_kind_and_timing() -> (
    None
):
    handler, _, _, _ = _handler(clock=lambda: OPENED_AT)
    completed, outcomes = _outcomes(handler, _event(as_of=TAG_PUSHED))
    assert isinstance(completed, ModelReceiptGatePinAdvanceCompleted)
    assert completed.disposition is EnumPinAdvanceDisposition.ADVANCED
    assert len(outcomes) == 6
    by_repo = {o.repository: o for o in outcomes}
    assert by_repo["OmniNode-ai/omnibase_infra"].pin_kind is EnumPinKind.SHA
    assert {
        o.pin_kind for r, o in by_repo.items() if r != "OmniNode-ai/omnibase_infra"
    } == {EnumPinKind.TAG}
    for outcome in outcomes:
        assert outcome.outcome is EnumPinOutcomeKind.OPENED
        assert outcome.core_tag == NEW.tag
        assert outcome.tag_pushed_at == TAG_PUSHED
        assert outcome.outcome_at == OPENED_AT
        assert outcome.seconds_tag_to_pr_opened == 90.0
        assert outcome.branch == f"pin-advance/receipt-gate/{NEW.tag}"
        assert outcome.branch_head_sha is not None
        assert outcome.pr_number is not None
        assert outcome.refusal_class is None


def test_the_handler_returns_the_completed_event_first_then_the_outcomes() -> None:
    handler, _, _, _ = _handler()
    emitted = asyncio.run(handler.handle(_event()))
    assert type(emitted[0]).__name__ == "ModelReceiptGatePinAdvanceCompleted"
    assert {type(e).__name__ for e in emitted[1:]} == {"ModelPinOutcomeEvent"}
    assert len(emitted) == 7


def test_a_refused_pull_request_emits_a_failed_outcome_classed_by_its_error() -> None:
    handler, _, _, _ = _handler(opener=FakeOpener(fail_for="OmniNode-ai/omniclaude"))
    _, outcomes = _outcomes(handler, _event(as_of=TAG_PUSHED))
    failed = [o for o in outcomes if o.outcome is EnumPinOutcomeKind.FAILED]
    assert [o.repository for o in failed] == ["OmniNode-ai/omniclaude"]
    assert failed[0].refusal_class is EnumPinRefusalClass.GUARD
    assert failed[0].seconds_tag_to_pr_opened is None
    assert failed[0].pr_number is None
    assert "quota_floor" in failed[0].detail
    assert len(outcomes) == 6


def test_the_error_text_names_its_refusal_class_from_the_contract_patterns() -> None:
    from omnimarket.nodes.node_receipt_gate_pin_advance_effect.handlers.handler_receipt_gate_pin_advance_effect import (
        classify_refusal,
    )

    config = load_effect_config()
    assert (
        classify_refusal(
            "POST /repos/x/git/refs: HTTP 422 Reference already exists", config
        )
        is EnumPinRefusalClass.CONFLICT
    )
    assert (
        classify_refusal("runner-routing audit refused the caller", config)
        is EnumPinRefusalClass.AUDIT
    )
    assert (
        classify_refusal("pr_create for x was refused: quota_floor", config)
        is EnumPinRefusalClass.GUARD
    )
    assert classify_refusal("socket reset", config) is EnumPinRefusalClass.OTHER


def test_a_caller_the_planner_could_not_judge_emits_a_refused_outcome() -> None:
    class EmptySpi(FakeRepository):
        def read_snapshot(self, caller: ModelPinCaller) -> ModelCallerSnapshot:
            snapshot = super().read_snapshot(caller)
            if caller.repository.endswith("omnibase_spi"):
                return snapshot.model_copy(update={"files": {}})
            return snapshot

    handler, _, _, _ = _handler(clock=lambda: OPENED_AT)
    handler._repository = EmptySpi(OLD)
    _, outcomes = _outcomes(handler, _event(as_of=TAG_PUSHED))
    refused = [o for o in outcomes if o.outcome is EnumPinOutcomeKind.REFUSED]
    assert [o.repository for o in refused] == ["OmniNode-ai/omnibase_spi"]
    assert refused[0].refusal_class is EnumPinRefusalClass.UNMATCHED
    assert refused[0].pin_kind is EnumPinKind.TAG
    assert refused[0].core_tag == NEW.tag
    assert refused[0].branch == f"pin-advance/receipt-gate/{NEW.tag}"
    assert "pin_site_unmatched" in refused[0].detail
    assert len(outcomes) == 6


def test_a_redelivered_tag_emits_replayed_outcomes() -> None:
    handler, _, _, _ = _handler()
    asyncio.run(handler.handle(_event()))
    _, again = _outcomes(handler, _event())
    assert {o.outcome for o in again} == {EnumPinOutcomeKind.REPLAYED}


def test_a_dry_run_and_an_ignored_tag_emit_no_outcome() -> None:
    _, dry = _outcomes(_handler(mode=EnumPinAdvanceMode.DRY_RUN)[0], _event())
    _, ignored = _outcomes(_handler()[0], _event(repo="OmniNode-ai/omnimarket"))
    _, current = _outcomes(_handler(NEW)[0], _event())
    assert dry == ignored == current == ()
