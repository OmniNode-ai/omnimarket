# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory stand-ins for the trigger's outside reads, and the bus rig the chain tests share."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    event_type_for,
)
from omnimarket.lab_work.bus import _bytes
from omnimarket.models.dev_merge_redeploy_overlay import (
    ModelDevMergeRedeployPolicy,
)
from omnimarket.models.model_repo_merged import ModelRepoMerged
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers import (
    DevMergeRedeployHost,
    HandlerDevMergeRedeployTrigger,
    HandlerDevMergeRedeployVerify,
    load_dev_merge_redeploy_topics,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.protocol_dev_merge_redeploy import (
    BuildContextRefUnreadableError,
    ClassifierUnavailableError,
    LaneUnreadableError,
    MergeFilesUnreadableError,
)

MERGE_SHA = "a" * 40
LANE_SHA_AT_MERGE = "a" * 40
LANE_SHA_OLDER = "9" * 40
BUILD_CONTEXT_SHA = "c" * 40
REPO = "OmniNode-ai/omnimarket"


def policy(
    deadline: float = 5.0, mode: str = "publish", poll: float = 0.02
) -> ModelDevMergeRedeployPolicy:
    return ModelDevMergeRedeployPolicy.model_validate(
        {
            "repos": [
                {
                    "repo": REPO,
                    "source_repo": "omnimarket",
                    "base_branches": ["dev"],
                    "clone_dir": "$OMNI_HOME/sibling",
                    "build_context_clone_dir": "$OMNI_HOME/build-context",
                    "build_context_branch": "dev",
                    "provenance_section": "siblings",
                }
            ],
            "classifier_path": "$OMNI_HOME/gate/classifier.py",
            "lane_probe_argv": ["probe"],
            "requested_by": "lab-node",
            "mode": mode,
            "observe_poll_seconds": poll,
            "verify_deadline_seconds": deadline,
        }
    )


def merged(
    base: str = "dev", repo: str = REPO, n: int = 7, sha: str = MERGE_SHA
) -> ModelRepoMerged:
    return ModelRepoMerged(
        repo=repo,
        base=base,
        merge_sha=sha,
        pr_number=n,
        merged_at=datetime.now(UTC),
        source="test",
    )


class FakeFiles:
    def __init__(
        self, files: Sequence[str] = ("src/omnimarket/x.py",), fail: bool = False
    ) -> None:
        self.files = tuple(files)
        self.fail = fail
        self.calls = 0

    def changed_files(
        self, clone_dir: str, base: str, merge_sha: str
    ) -> tuple[str, ...]:
        self.calls += 1
        if self.fail:
            raise MergeFilesUnreadableError("clone has no such commit")
        return self.files


class FakeClassifier:
    """Runtime iff the path starts with ``src/``."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def runtime_paths(
        self, repo: str, clone_dir: str, merge_sha: str, files: Sequence[str]
    ) -> tuple[str, ...]:
        if self.fail:
            raise ClassifierUnavailableError("classifier file is missing")
        return tuple(f for f in files if f.startswith("src/"))


class FakeBuildContext:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def resolve(self, clone_dir: str, branch: str) -> str:
        if self.fail:
            raise BuildContextRefUnreadableError("origin/dev did not resolve")
        return BUILD_CONTEXT_SHA


class FakeLane:
    def __init__(
        self,
        lane_sha: str = LANE_SHA_AT_MERGE,
        carries: bool = True,
        unreadable: bool = False,
        behind_reads: int = 0,
    ) -> None:
        self.behind_reads = behind_reads
        self.lane_sha = lane_sha
        self.carries = carries
        self.unreadable = unreadable
        self.reads = 0

    def vendored_revision(self, source_repo: str, section: str) -> str:
        self.reads += 1
        if self.unreadable:
            raise LaneUnreadableError("probe exited 255")
        return self.lane_sha

    def lane_carries(
        self, clone_dir: str, base: str, merge_sha: str, lane_sha: str
    ) -> bool:
        return self.carries and self.reads > self.behind_reads


class Bus(EventBusInmemory):
    """The in-memory bus; ``fail_topic`` makes publishing to that topic raise."""

    def __init__(self) -> None:
        super().__init__(environment="local", group="dev-merge-redeploy-test")
        self.fail_topic: str | None = None
        self.groups: list[tuple[str, str]] = []

    async def subscribe(
        self, topic: str, on_message: Callable[[Any], Awaitable[None]], group_id: str
    ) -> Callable[[], Awaitable[None]]:
        self.groups.append((topic, group_id))
        return await super().subscribe(topic, on_message=on_message, group_id=group_id)

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, *a: Any, **k: Any
    ) -> None:
        if topic == self.fail_topic:
            raise OSError("broker refused the write")
        await super().publish(topic, key, value, *a, **k)


class Rig:
    """A host on the in-memory bus with a recorder of every event the node produced, in order."""

    def __init__(
        self,
        *,
        files: FakeFiles | None = None,
        classifier: FakeClassifier | None = None,
        build_context: FakeBuildContext | None = None,
        lane: FakeLane | None = None,
        deadline: float = 5.0,
        mode: str = "publish",
    ) -> None:
        self.bus = Bus()
        self.policy = policy(deadline, mode)
        self.files = files or FakeFiles()
        self.lane = lane or FakeLane()
        trigger = HandlerDevMergeRedeployTrigger(
            self.policy,
            files=self.files,
            classifier=classifier or FakeClassifier(),
            build_context=build_context or FakeBuildContext(),
        )
        verify = HandlerDevMergeRedeployVerify(self.policy, lane=self.lane)
        self.host = DevMergeRedeployHost(
            self.bus, trigger, verify, self.policy, host_name="h-test"
        )
        self.topics = load_dev_merge_redeploy_topics()
        self.produced: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> Rig:
        await self.bus.start()

        async def record(topic: str) -> None:
            async def on(message: ProtocolBusMessage) -> None:
                self.produced.append((topic, json.loads(message.value)["payload"]))

            await self.bus.subscribe(topic, on, f"recorder-{topic}")

        await record(self.topics.start_command)
        await record(self.topics.receipt)
        await self.host.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.host.stop()
        await self.bus.close()

    async def publish_merge(self, event: ModelRepoMerged) -> None:
        await self.publish(
            self.topics.merged, event.model_dump(mode="json"), event.repo
        )

    async def publish_completed(
        self, cid: UUID | str, final_phase: str = "done", error: str | None = None
    ) -> None:
        payload = {
            "correlation_id": str(cid),
            "final_phase": final_phase,
            "phases_completed": 6,
            "error_message": error,
        }
        await self.publish(self.topics.completed, payload, str(cid))

    async def publish(self, topic: str, payload: dict[str, Any], key: str) -> None:
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=payload, event_type=event_type_for(topic)
        )
        await self.bus.publish(topic, key.encode(), _bytes(envelope))

    def topics_in_order(self) -> list[str]:
        return [topic for topic, _ in self.produced]

    def receipts(self) -> list[dict[str, Any]]:
        return [p for t, p in self.produced if t == self.topics.receipt]

    def commands(self) -> list[dict[str, Any]]:
        return [p for t, p in self.produced if t == self.topics.start_command]


async def wait_for(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)
