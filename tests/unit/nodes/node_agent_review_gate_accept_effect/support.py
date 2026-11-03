# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fakes and builders for the gate-accept node: files, reviews, verifier and two buses.

Nothing here reaches GitHub, a verifier or a broker. The two bus classes are the two
profiles' bindings: ``InMemoryBus`` delivers a publish straight to its subscribers;
``BrokerDouble`` keeps an ordered log per topic and delivers it from a consumer task
over serialized bytes, the way a broker binding does.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from omnibase_infra.event_bus.models.model_event_headers import ModelEventHeaders
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_agent_review_gate_accept_effect.models import (
    ModelReceiptVerifyOutcome,
    ModelReceiptVerifyRequest,
    ModelReviewView,
)

REPO = "example-org/example-repo"
PR = 41
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
PUBLISHER = "example-publisher[bot]"
VERIFIER_ENDPOINT = "http://verifier.test:8080"
RESULT_PATH = "/runner/temp/hostile-reviewer-result.json"
OUTPUT_PATH = "/runner/temp/github-output"
SUMMARY_PATH = "/runner/temp/github-summary"

CREDIT_ERROR = (
    "[ONEX_CORE_205_SERVICE_UNAVAILABLE] Server error (503) from review-gateway"
    ' :: response_body={"error":{"type":"gateway_keys_set_aside",'
    '"reason":"credit_exhausted","message":"every key was refused"}}'
)
UNAVAILABLE_ERROR = (
    "[ONEX_CORE_205_SERVICE_UNAVAILABLE] Server error (503) from review-gateway"
    ' :: response_body={"error":{"type":"gateway_no_key","message":"no key"}}'
)
TIMEOUT_ERROR = "[ONEX_CORE_092_TIMEOUT_ERROR] the review call timed out after 600s"


def voter(
    name: str, *, error: str | None = None, severities: tuple[str, ...] = ()
) -> dict[str, Any]:
    """One entry of the result file's ``results``: a failed voter has an error."""
    return {
        "model": name,
        "success": error is None,
        "error": error,
        "findings": [{"severity": severity} for severity in severities],
    }


def result_file(
    *voters: dict[str, Any], verdict: str | None = "degraded_quorum"
) -> str:
    """The voter result document, as ``cli_review`` writes it."""
    succeeded = [str(v["model"]) for v in voters if v["success"]]
    document: dict[str, Any] = {
        "models_attempted": [str(v["model"]) for v in voters],
        "models_succeeded": succeeded,
        "models_failed": [str(v["model"]) for v in voters if not v["success"]],
        "results": list(voters),
    }
    if verdict is not None:
        document["quorum"] = {"verdict": verdict, "blocking_count": 0}
    return json.dumps(document)


def degraded_with(*causes: str) -> str:
    """A degraded result in which every voter was lost to the given rendered errors."""
    return result_file(
        *(voter(f"voter-{i}", error=error) for i, error in enumerate(causes, start=1))
    )


def receipt_review(
    receipt_id: str,
    *,
    author: str = PUBLISHER,
    commit_id: str = HEAD,
    state: str = "COMMENTED",
) -> ModelReviewView:
    """A pull request review whose body carries one receipt block."""
    payload = {
        "schema_version": "hostile_review_agent_receipt.v1",
        "id": receipt_id,
        "repo": REPO,
        "pr": PR,
        "head_sha": commit_id,
    }
    body = f"Agent review.\n\n```onex-review-receipt\n{json.dumps(payload)}\n```\n"
    return ModelReviewView(
        author_login=author, commit_id=commit_id, state=state, body=body
    )


def plain_review(author: str = PUBLISHER) -> ModelReviewView:
    return ModelReviewView(
        author_login=author, commit_id=HEAD, state="COMMENTED", body="Looks fine."
    )


class FakeFiles:
    """The step's files, held as text by path; a missing path is unreadable."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files: dict[str, str] = dict(files or {})

    def read_text(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def append_text(self, path: str, text: str) -> None:
        self.files[path] = self.files.get(path, "") + text


class FakeReviews:
    def __init__(self, reviews: list[ModelReviewView], *, fail: bool = False) -> None:
        self._reviews = reviews
        self._fail = fail
        self.reads: list[tuple[str, int]] = []

    def reviews(self, repository: str, pull_request: int) -> list[ModelReviewView]:
        self.reads.append((repository, pull_request))
        if self._fail:
            raise ConnectionError("source control unreachable")
        return list(self._reviews)


class FakeVerifier:
    """Answers per receipt id; an id it was not told about is refused as unknown."""

    def __init__(
        self,
        outcomes: dict[str, ModelReceiptVerifyOutcome] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self.outcomes = outcomes or {}
        self.fail = fail
        self.calls: list[ModelReceiptVerifyRequest] = []

    def verify(self, request: ModelReceiptVerifyRequest) -> ModelReceiptVerifyOutcome:
        self.calls.append(request)
        if self.fail:
            raise ConnectionError("verifier unreachable")
        receipt_id = str(request.receipt.get("id", ""))
        return self.outcomes.get(
            receipt_id,
            ModelReceiptVerifyOutcome(accepted=False, reason="unknown_receipt"),
        )


def accepted(receipt_id: str) -> ModelReceiptVerifyOutcome:
    return ModelReceiptVerifyOutcome(accepted=True, receipt_id=receipt_id)


def refused(reason: str) -> ModelReceiptVerifyOutcome:
    return ModelReceiptVerifyOutcome(accepted=False, reason=reason)


def _message(topic: str, key: bytes | None, value: bytes) -> ModelEventMessage:
    return ModelEventMessage(
        topic=topic,
        key=key,
        value=value,
        headers=ModelEventHeaders(
            source="gate-accept-test", event_type=topic, timestamp=datetime.now(UTC)
        ),
    )


class InMemoryBus:
    """The in-memory binding: publishing calls the topic's subscribers in order."""

    def __init__(self) -> None:
        self.subscribers: dict[
            str, list[Callable[[ModelEventMessage], Awaitable[None]]]
        ] = {}

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> object:
        for callback in list(self.subscribers.get(topic, [])):
            await callback(_message(topic, key, value))
        return None

    async def subscribe_ungrouped(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
    ) -> Callable[[], Awaitable[None]]:
        self.subscribers.setdefault(topic, []).append(on_message)

        async def unsubscribe() -> None:
            self.subscribers[topic].remove(on_message)

        return unsubscribe


class BrokerDouble:
    """The Redpanda overlay's binding, without a broker.

    A publish appends to the topic's ordered log; each subscription is a consumer task
    that reads the log from its start and hands the bytes to the callback. A real
    broker leg is the lab step, not this unit test.
    """

    def __init__(self) -> None:
        self.logs: dict[str, list[tuple[bytes | None, bytes]]] = {}
        self._wake: dict[str, asyncio.Event] = {}

    def _event(self, topic: str) -> asyncio.Event:
        return self._wake.setdefault(topic, asyncio.Event())

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> object:
        self.logs.setdefault(topic, []).append((key, value))
        self._event(topic).set()
        return len(self.logs[topic]) - 1

    async def subscribe_ungrouped(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
    ) -> Callable[[], Awaitable[None]]:
        async def consume() -> None:
            offset = 0
            while True:
                log = self.logs.setdefault(topic, [])
                if offset < len(log):
                    key, value = log[offset]
                    offset += 1
                    await on_message(_message(topic, key, value))
                    continue
                event = self._event(topic)
                event.clear()
                if offset >= len(self.logs[topic]):
                    await event.wait()

        task = asyncio.create_task(consume())

        async def unsubscribe() -> None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        return unsubscribe
