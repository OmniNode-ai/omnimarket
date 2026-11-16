# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: request through the CLI main() with fake ports injected via the module's builder."""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_lab_proof_judge_use_effect import (
    runtime_lab_proof_judge_use as runtime,
)
from omnimarket.nodes.node_lab_proof_judge_use_effect.overlay import (
    ModelLabProofJudgeUseOverlay,
)
from tests.unit.nodes.node_lab_proof_judge_use_effect.conftest import (
    USE_TOPIC,
    FakeAppender,
    FakePublisher,
)

HEAD = "b" * 40


def _overlay() -> ModelLabProofJudgeUseOverlay:
    return ModelLabProofJudgeUseOverlay(
        schema_version=1,
        event={
            "topic": USE_TOPIC,
            "event_type": "lab_proof_judge.use_recorded",
        },
        ledger={
            "append": {
                "command_env": "EXAMPLE_LEDGER_APPEND_COMMAND",
                "host_env": "EXAMPLE_HOST_NAME",
                "timeout_seconds": 60,
            },
            "lane": "lab-proof-judge",
            "actor": "node:node_lab_proof_judge_use_effect",
            "ticket": "OMN-23642",
            "kind_cell": "lab-proof-class-use",
        },
    )


def _record_json() -> str:
    return json.dumps(
        {
            "repo": "example-org",
            "pr_number": 99,
            "head": HEAD,
            "class_id": "class-golden",
            "outcome": "not_applicable",
            "nodes": ["node-x", "node-y"],
            "receipt_ids": ["receipt-g1", "receipt-g2"],
            "observed_at": "2026-01-15T12:00:00+00:00",
        }
    )


def test_golden_chain_through_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    overlay = _overlay()
    appender = FakeAppender()
    publisher = FakePublisher()

    def fake_build_handler(
        overlay: ModelLabProofJudgeUseOverlay, environ: dict[str, str]
    ) -> Any:
        from omnimarket.nodes.node_lab_proof_judge_use_effect.handlers.handler_lab_proof_judge_use import (
            HandlerLabProofJudgeUse,
        )

        host = environ.get("EXAMPLE_HOST_NAME", "host-test")
        return HandlerLabProofJudgeUse(
            overlay=overlay, host=host, appender=appender, publisher=publisher
        )

    monkeypatch.setattr(runtime, "build_handler", fake_build_handler)
    monkeypatch.setattr(runtime, "load_overlay", lambda: overlay)
    monkeypatch.setattr("sys.stdin", _Stdin(_record_json()))

    exit_code = runtime.main(["record"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert result["event_published"] is True
    assert result["ledger"] == "written"
    assert len(publisher.events) == 1
    assert publisher.events[0]["topic"] == USE_TOPIC
    assert len(appender.rows) == 1
    assert "example-org#99" in appender.rows[0]
    assert HEAD in appender.rows[0]
    assert "class-golden" in appender.rows[0]
    assert "receipt-g1,receipt-g2" in appender.rows[0]


class _Stdin:
    """Minimal stdin replacement for the record verb."""

    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text
