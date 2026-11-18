# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled tick entry for the host retention node: host name, class default, exit codes, heartbeat."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelHostRetentionRequest,
    ModelHostRetentionResult,
)
from omnimarket.nodes.node_host_retention_effect.runtime_host_retention_tick import (
    main,
)

pytestmark = pytest.mark.unit


class FakeHandler:
    """Return a canned result and record the request."""

    def __init__(self, result: ModelHostRetentionResult) -> None:
        self.result = result
        self.requests: list[ModelHostRetentionRequest] = []

    def handle(self, request: ModelHostRetentionRequest) -> ModelHostRetentionResult:
        self.requests.append(request)
        return self.result


def _ok(host: str = "h-test", errors: tuple[str, ...] = ()) -> ModelHostRetentionResult:
    return ModelHostRetentionResult(
        host=host, dry_run=False, status="ok", errors=errors
    )


def _line(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, dict)
    return parsed


def test_probe_answers_before_anything_loads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_HOST_NAME", raising=False)
    assert main(["--probe"]) == 0
    assert _line(capsys) == {"status": "probe_ok"}


def test_host_comes_from_the_environment_and_only_the_uv_cache_class_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_HOST_NAME", "h-test")
    handler = FakeHandler(_ok())
    assert main([], handler=handler) == 0  # type: ignore[arg-type]
    assert [(r.host_name, r.classes, r.dry_run) for r in handler.requests] == [
        ("h-test", ("uv_cache",), False)
    ]
    line = _line(capsys)
    assert line["status"] == "ok"
    assert line["host"] == "h-test"
    assert line["classes"] == ["uv_cache"]


def test_the_host_option_wins_and_dry_run_reaches_the_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_HOST_NAME", "from-env")
    handler = FakeHandler(_ok("from-option"))
    assert main(["--host", "from-option", "--dry-run"], handler=handler) == 0  # type: ignore[arg-type]
    assert handler.requests[0].host_name == "from-option"
    assert handler.requests[0].dry_run is True
    assert _line(capsys)["host"] == "from-option"


def test_an_unnamed_host_exits_3_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_HOST_NAME", raising=False)
    handler = FakeHandler(_ok())
    assert main([], handler=handler) == 3  # type: ignore[arg-type]
    assert handler.requests == []
    assert _line(capsys)["status"] == "host_unnamed"


def test_a_refused_pass_exits_2_and_names_the_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_HOST_NAME", "h-test")
    refused = ModelHostRetentionResult(
        host="h-test", dry_run=False, status="refused", refusals=("cap too low",)
    )
    assert main([], handler=FakeHandler(refused)) == 2  # type: ignore[arg-type]
    line = _line(capsys)
    assert line["status"] == "refused"
    assert line["refusals"] == ["cap too low"]


def test_a_pass_with_errors_exits_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_HOST_NAME", "h-test")
    assert main([], handler=FakeHandler(_ok(errors=("boom",)))) == 3  # type: ignore[arg-type]
    assert _line(capsys)["errors"] == ["boom"]


def test_an_unreadable_overlay_is_a_refusal_exit_2_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_HOST_NAME", "h-test")
    monkeypatch.setenv("ONEX_HOST_RETENTION_OVERLAY", str(tmp_path / "absent.yaml"))
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    assert main([]) == 2
    line = _line(capsys)
    assert line["status"] == "refused"
    assert "unreadable" in str(line["refusals"])


def test_the_heartbeat_records_the_exit_code_and_the_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_HOST_NAME", "h-test")
    state = tmp_path / "state"
    assert main(["--state-dir", str(state)], handler=FakeHandler(_ok())) == 0  # type: ignore[arg-type]
    beat = json.loads(next(state.iterdir()).read_text(encoding="utf-8"))
    assert beat["exit_code"] == 0
    assert beat["result"]["host"] == "h-test"
