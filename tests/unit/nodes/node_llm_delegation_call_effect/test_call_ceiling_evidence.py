# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24087: every inference call of node_llm_delegation_call_effect logs one INFO line naming the call time allowance it was given.

The running runtime's log shows the overlay ceiling in force for each call.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelInferenceIntent

from omnimarket.handlers.routing.measured_correct_ceiling import (
    CODE_BUDGET_OVERLAY_ID,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)

pytestmark = pytest.mark.unit


class _ClassedIntent(ModelInferenceIntent):
    """The installed core model may not carry task_type yet."""

    task_type: str | None = None


def _overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ceiling_s: float,
    class_budgets: dict[str, float],
) -> None:
    """Write the code budget overlay under ``tmp_path`` and point the env root there."""
    path = tmp_path / CODE_BUDGET_OVERLAY_ID / "overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    budgets = "\n".join(f"  {k}: {v}" for k, v in class_budgets.items())
    path.write_text(
        (
            "class_budgets_s:\n"
            f"{budgets}\n"
            "sizing_margin: 1.5\n"
            "round_up_to_s: 60\n"
            "min_completed_samples: 3\n"
            f"ceiling_s: {ceiling_s}\n"
            "input_ceilings:\n"
            "  local-coder:\n"
            "    code_generation: 65536\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))


def _intent(
    *,
    task_type: str | None = None,
    timeout_seconds: float = 300.0,
    **overrides: object,
) -> _ClassedIntent:
    defaults: dict[str, object] = {
        "base_url": "http://localhost:8000/v1/chat/completions",
        "model": "Qwen3.8-27B",
        "system_prompt": "You are a helpful assistant.",
        "prompt": "Write a test.",
        "max_tokens": 512,
        "temperature": 0.3,
        "timeout_seconds": timeout_seconds,
        "correlation_id": uuid4(),
        "task_type": task_type,
    }
    defaults.update(overrides)
    return _ClassedIntent.model_validate(defaults)


@contextmanager
def _inference_server(*, stall_s: float = 0.0) -> Iterator[tuple[str, list[bytes]]]:
    """A real loopback chat-completions server; yields its URL and the bodies it received."""
    received: list[bytes] = []
    release = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            received.append(self.rfile.read(length))
            if stall_s:
                release.wait(stall_s)
                return
            body = json.dumps(
                {
                    "id": "chatcmpl-abc",
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions", received
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_code_generation_call_logs_the_overlay_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Golden: a code_generation call logs exactly one ceiling line.

    The failing control: at the merge base no line is logged so the assertion on exactly one line fails.
    """
    _overlay(
        tmp_path,
        monkeypatch,
        ceiling_s=600,
        class_budgets={"code_generation": 600},
    )
    intent = _intent(task_type="code_generation", timeout_seconds=300.0)
    with (
        _inference_server() as (url, _received),
        caplog.at_level(logging.INFO),
    ):
        result = HandlerInferenceIntent().handle(
            _intent(
                task_type="code_generation",
                timeout_seconds=300.0,
                base_url=url,
                correlation_id=intent.correlation_id,
            )
        )

    assert result.error_message in (None, "")
    ceiling_lines = [
        record.getMessage()
        for record in caplog.records
        if "inference call ceiling" in record.getMessage()
    ]
    assert len(ceiling_lines) == 1, caplog.text
    line = ceiling_lines[0]
    assert "task_type=code_generation" in line
    assert "ceiling_seconds=600" in line
    assert "requested_timeout_seconds=300.0" in line
    assert "resolved_timeout_seconds=300.0" in line
    assert str(intent.correlation_id) in line


def test_the_logged_ceiling_follows_the_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Golden: the logged ceiling follows the overlay value."""
    _overlay(
        tmp_path,
        monkeypatch,
        ceiling_s=600,
        class_budgets={"code_generation": 600},
    )
    intent = _intent(task_type="code_generation", timeout_seconds=300.0)
    with (
        _inference_server() as (url, _received),
        caplog.at_level(logging.INFO),
    ):
        HandlerInferenceIntent().handle(
            _intent(
                task_type="code_generation",
                timeout_seconds=300.0,
                base_url=url,
                correlation_id=intent.correlation_id,
            )
        )

    first_lines = [
        record.getMessage()
        for record in caplog.records
        if "inference call ceiling" in record.getMessage()
    ]
    assert len(first_lines) == 1
    assert "ceiling_seconds=600" in first_lines[0]

    caplog.clear()
    _overlay(
        tmp_path,
        monkeypatch,
        ceiling_s=600,
        class_budgets={"code_generation": 240},
    )
    with (
        _inference_server() as (url, _received),
        caplog.at_level(logging.INFO),
    ):
        HandlerInferenceIntent().handle(
            _intent(
                task_type="code_generation",
                timeout_seconds=300.0,
                base_url=url,
                correlation_id=intent.correlation_id,
            )
        )

    second_lines = [
        record.getMessage()
        for record in caplog.records
        if "inference call ceiling" in record.getMessage()
    ]
    assert len(second_lines) == 1
    assert "ceiling_seconds=240" in second_lines[0]
    assert "resolved_timeout_seconds=240.0" in second_lines[0]


def test_an_unnamed_task_type_is_refused_and_logs_no_ceiling_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Error chain: an unnamed task type is refused and logs no ceiling line."""
    _overlay(
        tmp_path,
        monkeypatch,
        ceiling_s=600,
        class_budgets={"reasoning": 300},
    )
    with (
        _inference_server() as (url, received),
        caplog.at_level(logging.INFO),
    ):
        result = HandlerInferenceIntent().handle(
            _intent(task_type="document", base_url=url)
        )

    assert "document" in result.error_message
    assert received == []
    ceiling_lines = [
        record.getMessage()
        for record in caplog.records
        if "inference call ceiling" in record.getMessage()
    ]
    assert len(ceiling_lines) == 0
