# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Facts-first HTTP delivery, with opt-in live readback (OMN-19432).

CI runs against a locally served endpoint through ``HandlerDelegateSkill`` ->
``LocalDelegationDispatchPort`` -> ``HandlerLlmDelegationCall`` -> a real HTTP
POST. The facts arm POSTs the facts, the plain arm does not, and every local
delegation terminates completed. The deterministic answer says nothing about
what a real model does with the facts; that is the opt-in live mode.

Three code-review prompts place a defect on a known new-file line well away
from 1. Each prompt runs under both arms:

* ``plain``: the contract's ``review`` shape is flipped to ``plain`` for the run;
* ``facts_first``: the shipped contract.

Each draw prints a ``FACTS-FIRST-LIVE`` line with the mode, status, cited lines,
and whether the known line is among them. Live outcomes depend on sampling;
the printed comparison is evidence, not an assertion about the model's answer.

Enable with::

    OMN_ALLOW_LIVE_LADDER=1 FACTS_FIRST_LIVE_ENDPOINT_URL=http://<host>:8000/v1/chat/completions \\
      FACTS_FIRST_LIVE_MODEL=Qwen3.8-27B uv run pytest -s \\
      tests/unit/nodes/node_delegate_skill_orchestrator/test_live_facts_first_omn19432.py
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.inference import task_class_authority as authority
from omnimarket.inference.task_class_authority import (
    _DEFAULT_AUTHORITY_PATH,
    ModelTaskClassAuthority,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_facts_first_prompt_compute.handlers.handler_facts_first_prompt import (
    FACTS_FIRST_HEADER,
)
from omnimarket.routing import delegation_backend_resolution

_LOCAL_MODEL = "facts-first-test"
_DRAWS = 3
_INSTRUCTION = (
    "Review this change. List each defect with the line number in the new file "
    "where it occurs. You must cite a line number for each defect. Do not invent "
    "defects. Answer in at most 80 words."
)

# (case id, old/new start line, rows, 1-based index into the rows of the defect)
_CASES: tuple[tuple[str, int, tuple[str, ...], int], ...] = (
    (
        "undefined-name",
        41,
        (
            " def total(items):",
            "     result = 0",
            "     for item in items:",
            "-        result += item.price",
            "+        result += item.price * item.qty",
            "+    result -= discount",
            "     return result",
        ),
        6,
    ),
    (
        "off-by-one",
        118,
        (
            " def last_n(values, n):",
            "     if n <= 0:",
            "         return []",
            "-    return values[-n:]",
            "+    return values[-n - 1 :]",
        ),
        5,
    ),
    (
        "inverted-condition",
        207,
        (
            " def allowed(user, resource):",
            "     if user.is_admin:",
            "         return True",
            "-    return resource.owner == user.id",
            "+    return resource.owner != user.id",
        ),
        5,
    ),
)


def _diff(start: int, rows: tuple[str, ...]) -> str:
    old = sum(1 for r in rows if r[0] in " -")
    new = sum(1 for r in rows if r[0] in " +")
    return f"```diff\n@@ -{start},{old} +{start},{new} @@\n" + "\n".join(rows) + "\n```"


def _defect_line(start: int, rows: tuple[str, ...], row_index: int) -> int:
    """The new-file line of the 1-based ``row_index``-th row."""
    return start + sum(1 for r in rows[: row_index - 1] if r[0] in " +")


def _cited(answer: str) -> set[int]:
    return {int(n) for n in re.findall(r"(?i)\blines?\s*#?(\d{1,5})\b", answer)}


class _ChatCompletionHandler(BaseHTTPRequestHandler):
    def _send_json(self, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json({"status": "ok"})
        elif self.path == "/v1/models":
            self._send_json(
                {
                    "object": "list",
                    "data": [{"id": _LOCAL_MODEL, "object": "model"}],
                }
            )
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        user_turn = next(
            message["content"]
            for message in reversed(payload["messages"])
            if message["role"] == "user"
        )
        hunk = re.search(r"@@ -\d+,\d+ \+(\d+)", user_turn)
        if hunk is None:
            self.send_error(400, "Expected a diff hunk")
            return
        answer = f"### ANSWER\nReview the change at line {hunk[1]} for a defect."
        prompt_tokens = len(user_turn.split())
        completion_tokens = len(answer.split())
        self._send_json(
            {
                "id": "chatcmpl-local-test",
                "object": "chat.completion",
                "created": 0,
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": answer},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
        )

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def endpoint() -> Iterator[tuple[str, str, str]]:
    url = os.environ.get("FACTS_FIRST_LIVE_ENDPOINT_URL", "").strip()
    if os.environ.get("OMN_ALLOW_LIVE_LADDER") == "1" and url:
        yield url, os.environ.get("FACTS_FIRST_LIVE_MODEL", "Qwen3.8-27B"), "live"
        return
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ChatCompletionHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            _LOCAL_MODEL,
            "served-locally",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _backends(url: str, model: str) -> list[dict[str, Any]]:
    return [
        {
            "backend_id": "live-facts-first",
            "endpoint_url": url,
            "model_name": model,
            "tier": "local",
            "max_tokens": 8192,
            "timeout_ms": 200000,
            "capabilities": ["review"],
        }
    ]


async def _delegate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prompt: str, url: str, model: str
) -> tuple[str, str, dict[str, Any]]:
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
        handler_llm_delegation_call as effect_module,
    )

    monkeypatch.setattr(
        delegation_backend_resolution,
        "load_bifrost_backends",
        lambda **_: _backends(url, model),
    )
    posted: dict[str, Any] = {}
    real_post = effect_module.transport.post_chat_completion

    def _observing_post(*, payload: dict[str, Any], **kwargs: Any) -> Any:
        posted.clear()
        posted.update(json.loads(json.dumps(payload)))
        return real_post(payload=payload, **kwargs)

    monkeypatch.setattr(
        effect_module.transport, "post_chat_completion", _observing_post
    )
    port = LocalDelegationDispatchPort(
        evidence_db_path=tmp_path / f"live-{uuid4()}.sqlite",
        effect_process_boundary=False,
    )
    response = await HandlerDelegateSkill(dispatch_port=port).handle(
        ModelDelegateSkillRequest(
            prompt=prompt,
            task_type="review",
            source="external-client",
            backend_id="live-facts-first",
            max_tokens=4096,
        )
    )
    return response.status, str(response.response or "").strip(), posted


def _with_review_shape(monkeypatch: pytest.MonkeyPatch, shape: str) -> None:
    raw = yaml.safe_load(_DEFAULT_AUTHORITY_PATH.read_text(encoding="utf-8"))
    raw["task_classes"]["review"]["prompt_shape"] = shape
    flipped = ModelTaskClassAuthority.model_validate(raw)
    monkeypatch.setattr(authority, "_delegation_task_class_authority", lambda: flipped)


@pytest.mark.parametrize("arm", ["plain", "facts_first"])
async def test_a_served_model_reviews_a_diff_under_each_prompt_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arm: str,
    endpoint: tuple[str, str, str],
) -> None:
    url, model, mode = endpoint
    _with_review_shape(monkeypatch, arm)
    delivered = 0
    right = 0
    draws = 0
    for case_id, start, rows, row_index in _CASES:
        known = _defect_line(start, rows, row_index)
        prompt = f"{_INSTRUCTION}\n\n{_diff(start, rows)}"
        for draw in range(_DRAWS):
            status, answer, posted = await _delegate(
                tmp_path, monkeypatch, prompt, url, model
            )
            user_turn = posted["messages"][-1]["content"]
            assert (FACTS_FIRST_HEADER in user_turn) is (arm == "facts_first")
            cited = _cited(answer)
            draws += 1
            delivered += status == "completed"
            right += known in cited
            print(
                f"FACTS-FIRST-LIVE mode={mode} arm={arm} case={case_id} draw={draw} "
                f"status={status} known_line={known} cited={sorted(cited)} "
                f"hit={known in cited}"
            )
    print(
        f"FACTS-FIRST-LIVE-TOTAL mode={mode} arm={arm} draws={draws} completed={delivered} "
        f"known_line_cited={right}"
    )
    assert draws == len(_CASES) * _DRAWS
    if mode == "served-locally":
        assert delivered == draws
