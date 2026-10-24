# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19174: abandoning inference must leave its client owned by the worker."""

from __future__ import annotations

import threading
from types import TracebackType

import httpx
import pytest

from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_inference_intent as inference,
)
from tests.unit.delegation.test_handler_inference_intent import _make_intent


@pytest.mark.unit
def test_abandoned_post_closes_its_client_only_on_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    in_flight = threading.Event()
    post_returned = threading.Event()
    closed = threading.Event()
    entered_threads: list[int] = []
    exited_threads: list[int] = []
    closing_threads: list[int] = []
    posting_threads: list[int] = []
    closed_while_in_flight: list[bool] = []
    caller_thread = threading.get_ident()

    class SilentClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self) -> SilentClient:
            entered_threads.append(threading.get_ident())
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            exited_threads.append(threading.get_ident())
            self.close()

        def close(self) -> None:
            closing_threads.append(threading.get_ident())
            closed_while_in_flight.append(in_flight.is_set())
            closed.set()

        def post(self, url: str, **kwargs: object) -> httpx.Response:
            posting_threads.append(threading.get_ident())
            in_flight.set()
            try:
                assert release.wait(5), "test did not release the silent upstream"
                return httpx.Response(
                    200,
                    request=httpx.Request("POST", url),
                    json={"choices": [{"message": {"content": "ok"}}]},
                )
            finally:
                in_flight.clear()
                post_returned.set()

    monkeypatch.setattr(inference.httpx, "Client", SilentClient)
    monkeypatch.setattr(
        inference.transport, "get_served_model_ids", lambda *_a, **_k: None
    )
    monkeypatch.setattr(inference, "_resolve_effective_timeout", lambda _intent: 0.2)

    try:
        with pytest.raises(RuntimeError, match="provider call timed out"):
            inference.HandlerInferenceIntent()._call_llm(
                _make_intent(),
                "omn19174",
                api_key=None,
                credential_source=None,
            )

        assert in_flight.is_set(), "the upstream must still be silent at abandonment"
        assert not closed.is_set(), "the caller closed an in-flight worker's client"
        assert closing_threads == []
        assert closed_while_in_flight == []
    finally:
        release.set()
        assert post_returned.wait(5), "the worker did not return from post"
        assert closed.wait(5), "the worker did not close its client"

    assert len(posting_threads) == 1
    assert posting_threads[0] != caller_thread
    assert entered_threads == posting_threads
    assert exited_threads == posting_threads
    assert closing_threads == posting_threads
    assert closed_while_in_flight == [False]
