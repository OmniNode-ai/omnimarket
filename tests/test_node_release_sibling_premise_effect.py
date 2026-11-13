# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the release sibling premise read effect (OMN-24209).

The handler under test meets fakes only -- no git, no broker, no classifier process:
branch history from planted dicts, a receipt source holding the same envelope bytes R2
publishes, sinks that keep (or refuse) what was published, a fixed clock. The golden
read walks to a runtime-affecting candidate and finds a VERIFIED folded rebuild among
its descendants; its control, the same facts with no descendants, shows the verdict
depends on the descendants the handler passes. Every broken read ends in exactly one
typed failure event and no verdict event. The overlay example and the receipt decoder
are pinned too.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    event_type_for,
)
from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
from omnimarket.lab_work.bus import _bytes
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.protocol_dev_merge_redeploy import (
    ClassifierUnavailableError,
    ProtocolMergeFiles,
    ProtocolRuntimePathClassifier,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models.model_dev_merge_redeploy import (
    EnumDevMergeMode,
    EnumDevMergeOutcome,
    ModelDevMergeRedeployReceipt,
)
from omnimarket.nodes.node_release_sibling_premise_compute.handlers.handler_sibling_premise import (
    HandlerSiblingPremise,
)
from omnimarket.nodes.node_release_sibling_premise_compute.models.enum_sibling_premise import (
    EnumSiblingPremiseState,
)
from omnimarket.nodes.node_release_sibling_premise_compute.models.model_sibling_premise_verdict import (
    ModelSiblingPremiseVerdict,
)
from omnimarket.nodes.node_release_sibling_premise_effect.handlers.adapter_receipt_source import (
    InMemoryReceiptSource,
    decode_receipt,
)
from omnimarket.nodes.node_release_sibling_premise_effect.handlers.handler_sibling_premise_read import (
    HandlerSiblingPremiseRead,
)
from omnimarket.nodes.node_release_sibling_premise_effect.handlers.lane_keywords import (
    lane_keywords,
)
from omnimarket.nodes.node_release_sibling_premise_effect.handlers.protocol_sibling_premise_read import (
    ProtocolBranchHistory,
    ProtocolReceiptSource,
    ProtocolVerdictSink,
)
from omnimarket.nodes.node_release_sibling_premise_effect.handlers.sink import (
    BusVerdictSink,
    InMemoryVerdictSink,
)
from omnimarket.nodes.node_release_sibling_premise_effect.models.model_sibling_premise_read_command import (
    ModelSiblingPremiseReadCommand,
)
from omnimarket.nodes.node_release_sibling_premise_effect.models.model_sibling_premise_read_failed import (
    EnumSiblingPremiseReadFailure,
    ModelSiblingPremiseReadFailed,
    SiblingPremiseReadError,
)
from omnimarket.nodes.node_release_sibling_premise_effect.models.model_sibling_premise_read_topics import (
    load_topics,
)
from omnimarket.nodes.node_release_sibling_premise_effect.overlay import (
    ModelSiblingPremiseReadOverlay,
    ModelSiblingPremiseRepo,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
OVERLAY_EXAMPLE = (
    REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_release_sibling_premise_effect"
    / "overlay.example.yaml"
)

REPO = "OmniNode-ai/omnimarket"
HOST = "h053"
CLONE = "/opt/clones/omnimarket"
BASE = "main"
LANE_REVISION = "9" * 40

#: The walk's newest commit is runtime-affecting and so is the candidate; the folded
#: rebuild that verified on the lane is a descendant of it.
SHA_CANDIDATE = "c" * 40
SHA_REBUILD = "e" * 40
SHA_DOCS = "d" * 40
SHA_OLDER = "b" * 40
SHA_RUNTIME = "5" * 40
SHA_OLDER_RUNTIME = "4" * 40
SHA_EXPLICIT = "7" * 40

NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)
MERGED_AT = NOW - timedelta(hours=2)

TOPICS = load_topics()
OVERLAY = ModelSiblingPremiseReadOverlay(
    repos=(ModelSiblingPremiseRepo(repo=REPO, clone_dir=CLONE, base_branch=BASE),),
    classifier_path="/opt/tools/deploy_path_classifier.py",
    git_timeout_seconds=30,
    classifier_timeout_seconds=60,
    pending_window_seconds=900,
    lookback_hours=24,
    max_walk_commits=3,
)
EXAMPLE_OVERLAY: dict[str, object] = yaml.safe_load(
    OVERLAY_EXAMPLE.read_text(encoding="utf-8")
)


class FakeHistory:
    """Branch history from planted dicts: a head, a walk, commit times, descendants."""

    def __init__(
        self,
        *,
        head: str,
        walk: tuple[str, ...],
        times: dict[str, datetime],
        descendants: dict[str, tuple[str, ...]],
    ) -> None:
        self._head = head
        self._walk = walk
        self._times = times
        self._descendants = descendants
        self.walks: list[tuple[str, str, int]] = []

    def head(self, clone_dir: str, branch: str) -> str:
        return self._head

    def first_parent_walk(
        self, clone_dir: str, branch: str, limit: int
    ) -> tuple[str, ...]:
        self.walks.append((clone_dir, branch, limit))
        return self._walk[:limit]

    def commit_time(self, clone_dir: str, sha: str) -> datetime:
        return self._times[sha]

    def descendants(self, clone_dir: str, branch: str, sha: str) -> tuple[str, ...]:
        return self._descendants.get(sha, ())


class FakeFiles:
    """The changed files of each sha, from a planted dict."""

    def __init__(self, files_by_sha: dict[str, tuple[str, ...]]) -> None:
        self._by_sha = files_by_sha
        self.asked: list[str] = []

    def changed_files(
        self, clone_dir: str, base: str, merge_sha: str
    ) -> tuple[str, ...]:
        self.asked.append(merge_sha)
        return self._by_sha.get(merge_sha, ())


class FakeClassifier:
    """The runtime paths of each sha, from a planted dict; () means none."""

    def __init__(self, runtime_by_sha: dict[str, tuple[str, ...]]) -> None:
        self._by_sha = runtime_by_sha
        self.asked: list[str] = []

    def runtime_paths(
        self, repo: str, clone_dir: str, merge_sha: str, files: Sequence[str]
    ) -> tuple[str, ...]:
        self.asked.append(merge_sha)
        return self._by_sha.get(merge_sha, ())


class UnavailableClassifier:
    """A classifier that cannot answer: the question is refused, never read as 'no'."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def runtime_paths(
        self, repo: str, clone_dir: str, merge_sha: str, files: Sequence[str]
    ) -> tuple[str, ...]:
        self.asked.append(merge_sha)
        raise ClassifierUnavailableError("the classifier script is missing")


class RaisingReceiptSource:
    """A receipt source whose read fails, as a dead broker does."""

    def __init__(self, error: SiblingPremiseReadError) -> None:
        self._error = error

    async def read(
        self, topic: str, since: datetime
    ) -> tuple[ModelDevMergeRedeployReceipt, ...]:
        raise self._error

    def skipped(self) -> int:
        return 0


class VerdictRefusingSink:
    """A sink that refuses every verdict publish and records every failure publish."""

    def __init__(self, refused_topic: str) -> None:
        self._refused_topic = refused_topic
        self.published: list[tuple[str, dict[str, object], UUID]] = []

    async def publish(
        self, topic: str, payload: dict[str, object], correlation_id: UUID
    ) -> None:
        if topic == self._refused_topic:
            raise RuntimeError(f"the broker refused {topic}")
        self.published.append((topic, payload, correlation_id))


def _receipt(
    *,
    outcome: EnumDevMergeOutcome,
    merge_sha: str,
    observed_at: datetime,
    lane_revision: str = "",
) -> ModelDevMergeRedeployReceipt:
    return ModelDevMergeRedeployReceipt(
        host=HOST,
        repo=REPO,
        pr_number=1,
        merge_sha=merge_sha,
        base=BASE,
        correlation_id=uuid4(),
        mode=EnumDevMergeMode.OBSERVE,
        outcome=outcome,
        command_published=False,
        runtime_paths=1,
        lane_revision=lane_revision,
        message=f"{outcome.value} happened",
        observed_at=observed_at,
    )


def _behind_receipt() -> ModelDevMergeRedeployReceipt:
    """R2's receipt for the candidate's own merge: the lane was behind it."""
    return _receipt(
        outcome=EnumDevMergeOutcome.LANE_BEHIND,
        merge_sha=SHA_CANDIDATE,
        observed_at=NOW - timedelta(minutes=90),
    )


def _verified_receipt(merge_sha: str = SHA_REBUILD) -> ModelDevMergeRedeployReceipt:
    """R2's receipt for a covering merge: the lane was verified on it."""
    return _receipt(
        outcome=EnumDevMergeOutcome.VERIFIED,
        merge_sha=merge_sha,
        observed_at=NOW - timedelta(hours=1),
        lane_revision=LANE_REVISION,
    )


def _receipt_envelope(receipt: ModelDevMergeRedeployReceipt) -> bytes:
    """The receipt as real envelope bytes, built the way R2's host publishes them."""
    envelope = ModelEventEnvelope[dict[str, object]](
        payload=receipt.model_dump(mode="json"),
        event_type=event_type_for(TOPICS.receipt),
        correlation_id=receipt.correlation_id,
    )
    return _bytes(envelope)


def _receipt_source(
    *receipts: ModelDevMergeRedeployReceipt,
) -> InMemoryReceiptSource:
    return InMemoryReceiptSource([_receipt_envelope(r) for r in receipts])


def _golden_history(
    *, descendants: dict[str, tuple[str, ...]] | None = None
) -> FakeHistory:
    """The walk of the golden facts; its first sha is runtime-affecting."""
    planted = {SHA_CANDIDATE: (SHA_REBUILD,)} if descendants is None else descendants
    return FakeHistory(
        head=SHA_CANDIDATE,
        walk=(SHA_CANDIDATE, SHA_DOCS, SHA_OLDER),
        times={SHA_CANDIDATE: MERGED_AT},
        descendants=planted,
    )


def _read(
    *,
    history: ProtocolBranchHistory | None = None,
    files: ProtocolMergeFiles | None = None,
    classifier: ProtocolRuntimePathClassifier | None = None,
    receipts: ProtocolReceiptSource | None = None,
    sink: ProtocolVerdictSink | None = None,
    overlay: ModelSiblingPremiseReadOverlay | None = None,
) -> HandlerSiblingPremiseRead:
    """A handler on the golden facts; every outside read is overridable."""
    return HandlerSiblingPremiseRead(
        overlay or OVERLAY,
        receipts=receipts or _receipt_source(_behind_receipt(), _verified_receipt()),
        history=history or _golden_history(),
        files=files
        or FakeFiles(
            {
                SHA_CANDIDATE: ("src/omnimarket/runtime.py",),
                SHA_DOCS: ("docs/notes.md",),
                SHA_OLDER: ("src/omnimarket/older.py",),
            }
        ),
        classifier=classifier
        or FakeClassifier(
            {
                SHA_CANDIDATE: ("src/omnimarket/runtime.py",),
                SHA_DOCS: (),
                SHA_OLDER: ("src/omnimarket/older.py",),
            }
        ),
        decide=HandlerSiblingPremise(),
        sink=sink or InMemoryVerdictSink(),
        clock=lambda: NOW,
        topics=TOPICS,
    )


async def _fails_with(
    sink: InMemoryVerdictSink | VerdictRefusingSink,
    read: HandlerSiblingPremiseRead,
    command: ModelSiblingPremiseReadCommand,
    code: EnumSiblingPremiseReadFailure,
) -> ModelSiblingPremiseReadFailed:
    """The read raises ``code`` and ends in exactly one failure event, no verdict."""
    with pytest.raises(SiblingPremiseReadError) as raised:
        await read.handle(command)
    assert raised.value.code is code
    assert len(sink.published) == 1
    topic, payload, correlation = sink.published[0]
    assert topic == TOPICS.failure
    assert isinstance(correlation, UUID)
    failed = ModelSiblingPremiseReadFailed.model_validate(payload)
    assert failed.code is code
    assert failed.repo == command.repo
    assert failed.observed_at == NOW
    return failed


async def _wait_for(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def test_golden_walk_head_runtime_and_verified_descendant_is_ready() -> None:
    sink = InMemoryVerdictSink()
    classifier = FakeClassifier(
        {
            SHA_CANDIDATE: ("src/omnimarket/runtime.py",),
            SHA_DOCS: (),
            SHA_OLDER: ("src/omnimarket/older.py",),
        }
    )
    history = _golden_history()
    verified = _verified_receipt()
    read = _read(
        history=history,
        classifier=classifier,
        sink=sink,
        receipts=_receipt_source(_behind_receipt(), verified),
    )

    verdict = await read.handle(ModelSiblingPremiseReadCommand(repo=REPO))

    assert verdict.state is EnumSiblingPremiseState.READY
    assert verdict.candidate_sha == SHA_CANDIDATE
    assert verdict.covering_merge_sha == SHA_REBUILD
    assert verdict.lane_revision == LANE_REVISION
    assert verdict.correlation_id == verified.correlation_id
    assert verdict.receipts_considered == 2
    assert history.walks == [(CLONE, BASE, OVERLAY.max_walk_commits)]
    assert classifier.asked == [SHA_CANDIDATE]
    assert len(sink.published) == 1
    topic, payload, correlation = sink.published[0]
    assert topic == TOPICS.verdict
    assert ModelSiblingPremiseVerdict.model_validate(payload) == verdict
    assert correlation == verified.correlation_id


async def test_failing_control_without_descendants_reads_behind() -> None:
    """The golden facts with the descendants plucked out: the READY of the golden run
    depends on the descendants the handler passes to the decision."""
    sink = InMemoryVerdictSink()
    read = _read(
        history=_golden_history(descendants={}),
        sink=sink,
        receipts=_receipt_source(_behind_receipt(), _verified_receipt()),
    )

    verdict = await read.handle(ModelSiblingPremiseReadCommand(repo=REPO))

    assert verdict.state is EnumSiblingPremiseState.BEHIND
    assert verdict.covering_merge_sha == SHA_CANDIDATE
    assert verdict.receipts_considered == 1
    assert len(sink.published) == 1
    topic, payload, _correlation = sink.published[0]
    assert topic == TOPICS.verdict
    assert ModelSiblingPremiseVerdict.model_validate(payload) == verdict


async def test_walk_skips_non_runtime_commits_and_stops_at_the_first_runtime_one() -> (
    None
):
    walk = (SHA_DOCS, SHA_RUNTIME, SHA_OLDER_RUNTIME)
    files = FakeFiles(
        {
            SHA_DOCS: ("docs/notes.md",),
            SHA_RUNTIME: ("src/runtime.py",),
            SHA_OLDER_RUNTIME: ("src/older.py",),
        }
    )
    classifier = FakeClassifier(
        {
            SHA_DOCS: (),
            SHA_RUNTIME: ("src/runtime.py",),
            SHA_OLDER_RUNTIME: ("src/older.py",),
        }
    )
    history = FakeHistory(
        head=SHA_DOCS,
        walk=walk,
        times={SHA_RUNTIME: MERGED_AT},
        descendants={},
    )
    sink = InMemoryVerdictSink()
    read = _read(
        history=history,
        files=files,
        classifier=classifier,
        sink=sink,
        receipts=_receipt_source(_verified_receipt(merge_sha=SHA_RUNTIME)),
    )

    verdict = await read.handle(ModelSiblingPremiseReadCommand(repo=REPO))

    assert verdict.state is EnumSiblingPremiseState.READY
    assert verdict.candidate_sha == SHA_RUNTIME
    assert verdict.covering_merge_sha == SHA_RUNTIME
    assert files.asked == [SHA_DOCS, SHA_RUNTIME]
    assert classifier.asked == [SHA_DOCS, SHA_RUNTIME]


async def test_an_explicit_candidate_sha_is_used_with_no_walk() -> None:
    history = FakeHistory(
        head=SHA_EXPLICIT,
        walk=(SHA_CANDIDATE,),
        times={SHA_EXPLICIT: MERGED_AT},
        descendants={},
    )
    files = FakeFiles({})
    classifier = FakeClassifier({})
    sink = InMemoryVerdictSink()
    read = _read(
        history=history,
        files=files,
        classifier=classifier,
        sink=sink,
        receipts=_receipt_source(_verified_receipt(merge_sha=SHA_EXPLICIT)),
    )

    verdict = await read.handle(
        ModelSiblingPremiseReadCommand(repo=REPO, candidate_sha=SHA_EXPLICIT)
    )

    assert verdict.state is EnumSiblingPremiseState.READY
    assert verdict.candidate_sha == SHA_EXPLICIT
    assert verdict.covering_merge_sha == SHA_EXPLICIT
    assert history.walks == []
    assert files.asked == []
    assert classifier.asked == []


async def test_an_unreadable_receipt_topic_publishes_the_typed_failure() -> None:
    sink = InMemoryVerdictSink()
    read = _read(
        receipts=RaisingReceiptSource(
            SiblingPremiseReadError(
                EnumSiblingPremiseReadFailure.RECEIPTS_UNREADABLE,
                "the broker is unreachable",
            )
        ),
        sink=sink,
    )
    await _fails_with(
        sink,
        read,
        ModelSiblingPremiseReadCommand(repo=REPO),
        EnumSiblingPremiseReadFailure.RECEIPTS_UNREADABLE,
    )


async def test_a_repo_not_in_the_overlay_publishes_the_typed_failure() -> None:
    sink = InMemoryVerdictSink()
    read = _read(sink=sink)
    await _fails_with(
        sink,
        read,
        ModelSiblingPremiseReadCommand(repo="OmniNode-ai/absent"),
        EnumSiblingPremiseReadFailure.REPO_NOT_IN_OVERLAY,
    )


async def test_no_runtime_ancestor_within_the_walk_publishes_the_typed_failure() -> (
    None
):
    walk = ("1" * 40, "2" * 40, "3" * 40)
    history = FakeHistory(head="1" * 40, walk=walk, times={}, descendants={})
    files = FakeFiles({sha: (f"src/{sha[:1]}.py",) for sha in walk})
    classifier = FakeClassifier(dict.fromkeys(walk, ()))
    sink = InMemoryVerdictSink()
    read = _read(history=history, files=files, classifier=classifier, sink=sink)

    await _fails_with(
        sink,
        read,
        ModelSiblingPremiseReadCommand(repo=REPO),
        EnumSiblingPremiseReadFailure.NO_RUNTIME_ANCESTOR,
    )

    assert history.walks == [(CLONE, BASE, OVERLAY.max_walk_commits)]
    assert files.asked == list(walk)
    assert classifier.asked == list(walk)


async def test_a_classifier_that_cannot_answer_publishes_the_typed_failure() -> None:
    sink = InMemoryVerdictSink()
    classifier = UnavailableClassifier()
    read = _read(classifier=classifier, sink=sink)

    await _fails_with(
        sink,
        read,
        ModelSiblingPremiseReadCommand(repo=REPO),
        EnumSiblingPremiseReadFailure.CLASSIFIER_UNAVAILABLE,
    )

    assert classifier.asked == [SHA_CANDIDATE]


async def test_a_refused_verdict_publish_publishes_the_typed_failure() -> None:
    sink = VerdictRefusingSink(TOPICS.verdict)
    read = _read(sink=sink)
    await _fails_with(
        sink,
        read,
        ModelSiblingPremiseReadCommand(repo=REPO),
        EnumSiblingPremiseReadFailure.PUBLISH_FAILED,
    )


async def test_golden_chain_event_and_error_chain_event_on_the_in_memory_bus() -> None:
    verdict_events: list[bytes] = []
    failure_events: list[bytes] = []

    async def on_verdict(message: ProtocolBusMessage) -> None:
        verdict_events.append(message.value)

    async def on_failure(message: ProtocolBusMessage) -> None:
        failure_events.append(message.value)

    async with open_lab_run_bus(
        **lane_keywords(open_lab_run_bus, "inmemory", None, None, None)
    ) as bus:
        await bus.subscribe(
            TOPICS.verdict,
            on_message=on_verdict,
            group_id="sibling-premise-read-verdict-test",
        )
        await bus.subscribe(
            TOPICS.failure,
            on_message=on_failure,
            group_id="sibling-premise-read-failure-test",
        )
        read = _read(sink=BusVerdictSink(bus))

        verdict = await read.handle(ModelSiblingPremiseReadCommand(repo=REPO))
        await _wait_for(lambda: len(verdict_events) == 1)
        assert verdict.state is EnumSiblingPremiseState.READY
        assert len(failure_events) == 0

        with pytest.raises(SiblingPremiseReadError):
            await read.handle(ModelSiblingPremiseReadCommand(repo="OmniNode-ai/absent"))
        await _wait_for(lambda: len(failure_events) == 1)
        assert len(verdict_events) == 1

    verdict_envelope = json.loads(verdict_events[0])
    assert verdict_envelope["event_type"] == event_type_for(TOPICS.verdict)
    assert (
        ModelSiblingPremiseVerdict.model_validate(verdict_envelope["payload"])
        == verdict
    )

    failure_envelope = json.loads(failure_events[0])
    assert failure_envelope["event_type"] == event_type_for(TOPICS.failure)
    failed = ModelSiblingPremiseReadFailed.model_validate(failure_envelope["payload"])
    assert failed.code is EnumSiblingPremiseReadFailure.REPO_NOT_IN_OVERLAY
    assert failed.repo == "OmniNode-ai/absent"


def test_the_example_overlay_loads() -> None:
    overlay = ModelSiblingPremiseReadOverlay.model_validate(
        yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    )
    assert overlay.repo("omnimarket/platform") == ModelSiblingPremiseRepo(
        repo="omnimarket/platform",
        clone_dir="$OMNI_HOME/clones/platform",
        base_branch="main",
    )
    assert overlay.repo("omnimarket/infra") is not None
    assert overlay.lookback_hours == 24
    assert overlay.max_walk_commits == 50
    assert overlay.pending_window_seconds == 900


@pytest.mark.parametrize("key", sorted(EXAMPLE_OVERLAY))
def test_the_example_refuses_a_load_without_any_one_top_level_key(
    key: str,
) -> None:
    raw = {name: value for name, value in EXAMPLE_OVERLAY.items() if name != key}
    with pytest.raises(ValidationError):
        ModelSiblingPremiseReadOverlay.model_validate(raw)


def test_the_example_refuses_a_load_with_an_unknown_key() -> None:
    raw = dict(EXAMPLE_OVERLAY)
    raw["bogus"] = True
    with pytest.raises(ValidationError):
        ModelSiblingPremiseReadOverlay.model_validate(raw)


def test_overlay_repo_returns_none_for_an_absent_repo() -> None:
    overlay = ModelSiblingPremiseReadOverlay.model_validate(dict(EXAMPLE_OVERLAY))
    assert overlay.repo("OmniNode-ai/absent") is None


@pytest.mark.parametrize(
    "value", [b"", b"not json at all", b'{"unterminated": ', b"\xff\xfe\xfd"]
)
def test_decode_receipt_returns_none_for_bytes_that_are_not_json(value: bytes) -> None:
    assert decode_receipt(value) is None


def test_decode_receipt_returns_none_for_an_envelope_whose_payload_is_not_a_receipt() -> (
    None
):
    envelope = ModelEventEnvelope[dict[str, object]](
        payload={"repo": REPO},
        event_type=event_type_for(TOPICS.receipt),
        correlation_id=uuid4(),
    )
    assert decode_receipt(_bytes(envelope)) is None


def test_the_read_command_exits_78_when_the_overlay_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No overlay, no packaged default: the command refuses with exit 78 and reads nothing."""
    from click.testing import CliRunner

    from omnimarket.nodes.node_release_sibling_premise_effect.__main__ import group

    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    result = CliRunner().invoke(
        group,
        [
            "read",
            "--bus",
            "inmemory",
            "--omnibase-path",
            str(tmp_path),
            "--repo",
            "OmniNode-ai/example",
        ],
    )
    assert result.exit_code == 78


def test_terminal_event_topics_are_the_declared_states() -> None:
    topics = load_topics()
    assert topics.verdict == "onex.evt.omnimarket.release-sibling-premise-read.v1"
    assert (
        topics.failure == "onex.evt.omnimarket.release-sibling-premise-read-failed.v1"
    )
