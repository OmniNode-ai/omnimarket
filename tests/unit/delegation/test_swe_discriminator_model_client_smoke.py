# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Offline coverage of model routing, transport, and smoke orchestration."""

from __future__ import annotations

import argparse
import io
import json
import time
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from types import FunctionType, SimpleNamespace
from typing import Protocol, cast
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from omnimarket.delegation.graded_ladder.models import ModelLadderRung
from omnimarket.delegation.swe_discriminator import (
    arm_runner,
    model_client,
    run_smoke,
)
from omnimarket.delegation.swe_discriminator.models import (
    ArmRun,
    EnumArm,
    EnumRouting,
    EnumRunOutcome,
    GradedRow,
    ModelCall,
    ModelSweDiscriminatorRuntimeConfig,
    SmokeReport,
    SweTask,
)

pytestmark = pytest.mark.unit

_URL = "https://model.example.invalid/chat/completions"
_CODE = "def add(a, b):\n    return a + b\n"


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(model_client, "_OVERLAY_PATH", tmp_path / "overlay.yaml")
    monkeypatch.setattr(
        urllib.request, "urlopen", Mock(side_effect=AssertionError("network forbidden"))
    )
    monkeypatch.setattr(
        time, "sleep", Mock(side_effect=AssertionError("sleep forbidden"))
    )
    monkeypatch.setattr(
        run_smoke,
        "Settings",
        Mock(
            return_value=Mock(
                llm_glm_api_key=SecretStr(""), llm_coder_url="", llm_coder_model_id=""
            )
        ),
    )


@pytest.fixture
def rung() -> ModelLadderRung:
    return ModelLadderRung(
        rung_id="test-rung",
        order=0,
        model_name="test-model",
        backend_id="test-backend",
        endpoint_url_env="TEST_ENDPOINT",
        gpu="test",
        host_label="test",
    )


@pytest.fixture
def task() -> SweTask:
    return SweTask(
        task_id="add-task",
        level=1,
        source_pr="#1",
        source_sha="fixture",
        task_text="Implement add.",
        context_code="",
        required_defs=["add"],
        held_back_asserts="assert add(2, 3) == 5",
    )


def _call(
    *, content: str = _CODE, status: int = 200, error: str = "", finish: str = "stop"
) -> ModelCall:
    return ModelCall(
        role="monolith",
        tier="frontier",
        model_name="test-model",
        endpoint_label="test-label",
        prompt_chars=4,
        content=content,
        http_status=status,
        error=error,
        finish_reason=finish,
        cost_usd=0.25,
        latency_ms=12,
    )


def _run(task: SweTask, call: ModelCall) -> ArmRun:
    arm = EnumArm.A_MONOLITH_FRONTIER
    return ArmRun(
        task_id=task.task_id,
        arm=arm,
        decomposition=arm.decomposition,
        routing=arm.routing,
        artifact=call.content,
        calls=[call],
        total_cost_usd=call.cost_usd,
        total_latency_ms=call.latency_ms,
        decomposition_tax_usd=0.05,
        n_slices=1,
        blocked=model_client.is_infra_block(call),
        error=call.error,
    )


@pytest.mark.parametrize(
    ("overlay", "endpoint", "model"),
    [
        (None, None, "default"),
        ("", None, "default"),
        ("backends: null", None, "default"),
        ("backends: [{backend_id: other, model_name: other}]", None, "default"),
        (
            "backends: [{backend_id: target, endpoint_url: '', model_name: ''}]",
            None,
            "default",
        ),
        (
            f"backends: [{{backend_id: target, endpoint_url: '{_URL}', model_name: override}}]",
            _URL,
            "override",
        ),
    ],
    ids=[
        "missing-file",
        "empty-file",
        "null-backends",
        "missing-entry",
        "empty-values",
        "entry",
    ],
)
def test_overlay(overlay: str | None, endpoint: str | None, model: str) -> None:
    if overlay is not None:
        model_client._OVERLAY_PATH.write_text(overlay)
    assert model_client._overlay_endpoint("target") == endpoint
    assert model_client._overlay_model("target", "default") == model


def test_rung_lookup(monkeypatch: pytest.MonkeyPatch, rung: ModelLadderRung) -> None:
    monkeypatch.setattr(model_client, "load_rungs", lambda: [rung])
    assert model_client._rung_by_id(rung.rung_id) is rung
    with pytest.raises(
        RuntimeError, match=r"rung 'unknown' not found in ladder_rungs\.yaml"
    ):
        model_client._rung_by_id("unknown")


@pytest.mark.parametrize(
    "source", ["committed", "env", "backend", "overlay", "missing"]
)
def test_endpoint_precedence(
    source: str, monkeypatch: pytest.MonkeyPatch, rung: ModelLadderRung
) -> None:
    config = ModelSweDiscriminatorRuntimeConfig(
        endpoint_urls_by_env={"TEST_ENDPOINT": "env-url"}
        if source in ("committed", "env")
        else {},
        endpoint_urls_by_backend_id={"test-backend": "backend-url"}
        if source in ("committed", "env", "backend")
        else {},
    )
    overlay = Mock(return_value="overlay-url" if source != "missing" else None)
    monkeypatch.setattr(model_client, "_overlay_endpoint", overlay)
    if source == "committed":
        rung = rung.model_copy(update={"endpoint_url": "committed-url"})
    expected = None if source == "missing" else f"{source}-url"
    assert model_client._resolve_rung_endpoint(rung, config) == expected
    if source in ("overlay", "missing"):
        overlay.assert_called_once_with("test-backend")
    else:
        overlay.assert_not_called()


@pytest.mark.parametrize("tier", list(EnumRouting))
@pytest.mark.parametrize("custom_rung", [False, True])
def test_resolve_tier(
    tier: EnumRouting,
    custom_rung: bool,
    monkeypatch: pytest.MonkeyPatch,
    rung: ModelLadderRung,
) -> None:
    rung = rung.model_copy(
        update={
            "endpoint_url": _URL,
            "api_key_env": "TEST_KEY",
            "extra_headers": {"X-Test": "extra"},
        }
    )
    lookup = Mock(return_value=rung)
    monkeypatch.setattr(model_client, "_rung_by_id", lookup)
    monkeypatch.setattr(
        model_client, "_overlay_model", lambda _backend, _default: "overlay-model"
    )
    config = ModelSweDiscriminatorRuntimeConfig(
        api_keys_by_env={"TEST_KEY": "test-key"}
    )
    if custom_rung:
        config.frontier_rung_id = "custom-frontier"
        config.cost_rung_id = "custom-cost"
        config.model_names_by_backend_id = {"test-backend": "runtime-model"}
    url, model, label, headers, tier_name = model_client.resolve_tier(tier, config)
    assert url == _URL
    assert tier_name == tier.value
    if tier is EnumRouting.FRONTIER:
        lookup.assert_called_once_with(
            "custom-frontier" if custom_rung else "rung_cloud_glm"
        )
        assert model == "test-model"
        assert headers == {
            "Content-Type": "application/json",
            "Authorization": "Bearer test-key",
            "X-Test": "extra",
        }
    else:
        lookup.assert_called_once_with(
            "custom-cost" if custom_rung else "rung_5090_coder"
        )
        assert model == ("runtime-model" if custom_rung else "overlay-model")
        assert headers == {"Content-Type": "application/json"}
    assert label == f"{tier.value}:{model}"


@pytest.mark.parametrize("tier", list(EnumRouting))
def test_resolve_tier_missing_endpoint(
    tier: EnumRouting, monkeypatch: pytest.MonkeyPatch, rung: ModelLadderRung
) -> None:
    monkeypatch.setattr(model_client, "_rung_by_id", lambda _rung_id: rung)
    with pytest.raises(
        RuntimeError, match=f"{tier.value} rung 'test-rung' has no endpoint"
    ):
        model_client.resolve_tier(tier)


def test_frontier_key_required_only_for_authenticated_rung(
    monkeypatch: pytest.MonkeyPatch, rung: ModelLadderRung
) -> None:
    rung = rung.model_copy(update={"endpoint_url": _URL, "api_key_env": "TEST_KEY"})
    monkeypatch.setattr(model_client, "_rung_by_id", lambda _rung_id: rung)
    with pytest.raises(
        RuntimeError, match="frontier rung needs runtime config for TEST_KEY"
    ):
        model_client.resolve_tier(EnumRouting.FRONTIER)
    rung = rung.model_copy(update={"api_key_env": ""})
    assert model_client.resolve_tier(EnumRouting.FRONTIER)[3] == {
        "Content-Type": "application/json"
    }


@pytest.mark.parametrize(
    ("tier", "rates"),
    [
        (EnumRouting.FRONTIER, (0.60e-6, 2.20e-6)),
        (EnumRouting.COST_ROUTED, (0.05e-6, 0.05e-6)),
    ],
)
def test_rates(tier: EnumRouting, rates: tuple[float, float]) -> None:
    assert model_client._rates(tier) == pytest.approx(rates)


@pytest.mark.parametrize("status", [0, 429, 502, 503, 200, 400, 401, 500])
@pytest.mark.parametrize("error", ["", "failed"])
def test_infra_block_requires_error_and_infra_status(status: int, error: str) -> None:
    assert model_client.is_infra_block(_call(status=status, error=error)) is (
        bool(error) and status in (0, 429, 502, 503)
    )


class _Response(io.BytesIO):
    status = 200


class _OfflineChat(Protocol):
    def __call__(
        self,
        tier: EnumRouting,
        prompt: str,
        *,
        role: str,
        runtime_config: ModelSweDiscriminatorRuntimeConfig | None = None,
        max_tokens: int = 16384,
        timeout_s: float = 300.0,
        retries: int = 4,
    ) -> ModelCall: ...


@pytest.fixture
def chat_transport() -> Mock:
    return Mock()


@pytest.fixture
def chat_clock() -> Mock:
    ticks = iter([10.0, 10.125])
    return Mock(
        monotonic=Mock(side_effect=lambda: next(ticks)),
        sleep=Mock(side_effect=AssertionError("sleep forbidden")),
    )


@pytest.fixture
def offline_chat(chat_transport: Mock, chat_clock: Mock) -> _OfflineChat:
    # Execute the production code object with explicitly injected dependencies.
    # Unlike patching the production callable, this callable never holds a real
    # urlopen or the real routing resolver in its globals, even before a test.
    dependencies = {
        "json": json,
        "time": chat_clock,
        "urllib": SimpleNamespace(
            request=SimpleNamespace(
                Request=urllib.request.Request, urlopen=chat_transport
            ),
            error=SimpleNamespace(URLError=urllib.error.URLError),
        ),
        "resolve_tier": lambda tier, _config: (
            _URL,
            "test-model",
            "test-label",
            {"Content-Type": "application/json"},
            tier.value,
        ),
        "_rates": model_client._rates,
        "ModelCall": ModelCall,
        "ModelSweDiscriminatorRuntimeConfig": ModelSweDiscriminatorRuntimeConfig,
    }
    injected = FunctionType(model_client.chat.__code__, dependencies, "offline_chat")
    injected.__kwdefaults__ = model_client.chat.__kwdefaults__
    return cast(_OfflineChat, injected)


@pytest.mark.parametrize("tier", list(EnumRouting))
def test_chat_success(
    tier: EnumRouting, offline_chat: _OfflineChat, chat_transport: Mock
) -> None:
    response = _Response(
        json.dumps(
            {
                "choices": [
                    {"message": {"content": "answer"}, "finish_reason": "length"}
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 200},
            }
        ).encode()
    )
    chat_transport.return_value = response
    config = ModelSweDiscriminatorRuntimeConfig(max_retries=1, max_tokens=512)
    call = offline_chat(
        tier,
        "hello",
        role="worker",
        runtime_config=config,
        max_tokens=1024,
        timeout_s=7,
    )
    request = chat_transport.call_args.args[0]
    assert isinstance(request, urllib.request.Request)
    assert request.full_url == _URL
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert isinstance(request.data, bytes)
    assert json.loads(request.data) == {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.0,
        "max_tokens": 512,
    }
    chat_transport.assert_called_once_with(request, timeout=7)
    assert call.role == "worker"
    assert call.tier == tier.value
    assert call.model_name == "test-model"
    assert call.endpoint_label == "test-label"
    assert call.prompt_chars == 5
    assert call.content == "answer"
    assert (call.prompt_tokens, call.completion_tokens) == (100, 200)
    assert call.latency_ms == 125
    assert call.http_status == 200
    assert call.finish_reason == "length"
    assert call.cost_usd == pytest.approx(
        0.0005 if tier is EnumRouting.FRONTIER else 0.000015
    )
    assert call.error == ""
    assert response.closed


@pytest.mark.parametrize(
    "status",
    [400, 429, 503, 0],
    ids=["http-error", "rate-limit", "unavailable", "timeout"],
)
def test_chat_errors_are_captured(
    status: int, offline_chat: _OfflineChat, chat_transport: Mock
) -> None:
    error = (
        urllib.error.HTTPError(_URL, status, "fixture failure", Message(), None)
        if status
        else TimeoutError("fixture timeout")
    )
    chat_transport.side_effect = error
    call = offline_chat(EnumRouting.FRONTIER, "hello", role="monolith", retries=1)
    assert chat_transport.call_count == 1
    assert call.http_status == status
    assert call.error == str(error)
    assert call.content == ""
    assert call.prompt_chars == 5
    assert call.latency_ms == 125
    assert call.cost_usd == 0
    assert model_client.is_infra_block(call) is (status != 400)


@pytest.mark.parametrize("status", [429, 503])
def test_chat_retries_with_backoff(
    status: int, offline_chat: _OfflineChat, chat_transport: Mock, chat_clock: Mock
) -> None:
    chat_transport.side_effect = [
        urllib.error.HTTPError(_URL, status, "retry", Message(), None),
        _Response(b'{"choices": [{"message": {"content": "ok"}}]}'),
    ]
    chat_clock.sleep.side_effect = None
    call = offline_chat(
        EnumRouting.FRONTIER,
        "hello",
        role="worker",
        runtime_config=ModelSweDiscriminatorRuntimeConfig(max_retries=2),
        retries=1,
    )
    assert chat_transport.call_count == 2
    chat_clock.sleep.assert_called_once_with(20.0 if status == 429 else 3.0)
    assert call.content == "ok"
    assert call.error == ""


def test_chat_empty_choices(offline_chat: _OfflineChat, chat_transport: Mock) -> None:
    chat_transport.return_value = _Response(b'{"choices": []}')
    call = offline_chat(EnumRouting.FRONTIER, "hello", role="worker", retries=1)
    assert call.http_status == 200
    assert call.error == "malformed response: empty 'choices'"
    assert not model_client.is_infra_block(call)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", {}),
        (" , ", {}),
        (" A = one ,B=two=three,,A=last ", {"A": "last", "B": "two=three"}),
        ("EMPTY=", {"EMPTY": ""}),
    ],
)
def test_parse_key_values(raw: str, expected: dict[str, str]) -> None:
    assert run_smoke._parse_key_values(raw) == expected


def test_parse_key_values_rejects_missing_separator() -> None:
    with pytest.raises(ValueError, match="expected KEY=VALUE item, got 'broken'"):
        run_smoke._parse_key_values("A=ok,broken")


@pytest.mark.parametrize("cli_overrides", [False, True])
def test_runtime_config(cli_overrides: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Mock(
        return_value=Mock(
            llm_glm_api_key=SecretStr("settings-key"),
            llm_coder_url="settings-url",
            llm_coder_model_id="settings-model",
        )
    )
    monkeypatch.setattr(run_smoke, "Settings", settings)
    args = argparse.Namespace(
        frontier_api_key="cli-key" if cli_overrides else "",
        local_endpoint_url="cli-url" if cli_overrides else "",
        local_model="cli-model" if cli_overrides else "",
        api_key="OTHER_KEY=other,LLM_GLM_API_KEY=explicit",
        endpoint_url="BIFROST_LOCAL_CODER_ENDPOINT_URL=explicit-url,OTHER_ENDPOINT=other-url",
        model_name="other-backend=other-model",
        frontier_rung="frontier-fixture",
        cost_rung="local-fixture",
        decomposer_tier="cost_routed",
        max_retries=2,
        max_tokens=512,
    )
    config = run_smoke._runtime_config(args)
    settings.assert_called_once_with(_env_file=None)
    assert config.frontier_rung_id == "frontier-fixture"
    assert config.cost_rung_id == "local-fixture"
    assert config.api_keys_by_env == {
        "OTHER_KEY": "other",
        "LLM_GLM_API_KEY": "explicit",
    }
    assert config.endpoint_urls_by_env == {
        "BIFROST_LOCAL_CODER_ENDPOINT_URL": "explicit-url",
        "OTHER_ENDPOINT": "other-url",
    }
    assert config.endpoint_urls_by_backend_id == {
        "local-qwen-coder-30b": "cli-url" if cli_overrides else "settings-url"
    }
    assert config.model_names_by_backend_id == {
        "other-backend": "other-model",
        "local-qwen-coder-30b": "cli-model" if cli_overrides else "settings-model",
    }
    assert config.decomposer_tier is EnumRouting.COST_ROUTED
    assert (config.max_retries, config.max_tokens) == (2, 512)
    args.api_key = ""
    assert run_smoke._runtime_config(args).api_keys_by_env == {
        "LLM_GLM_API_KEY": "cli-key" if cli_overrides else "settings-key"
    }


@pytest.mark.parametrize(
    ("content", "status", "error", "finish", "outcome"),
    [
        (_CODE, 200, "", "stop", EnumRunOutcome.PASS),
        ("def add(a, b):\n    return 0\n", 200, "", "stop", EnumRunOutcome.FAIL_WRONG),
        ("", 429, "rate limited", "", EnumRunOutcome.BLOCKED),
        ("", 200, "", "length", EnumRunOutcome.TRUNCATED),
        ("  ", 200, "", "stop", EnumRunOutcome.NO_ARTIFACT),
    ],
    ids=["pass", "wrong", "blocked", "truncated", "no-artifact"],
)
def test_grade_phase(
    task: SweTask,
    content: str,
    status: int,
    error: str,
    finish: str,
    outcome: EnumRunOutcome,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = _run(task, _call(content=content, status=status, error=error, finish=finish))
    (row,) = run_smoke._grade_phase([task], [(2, run)])
    assert (row.task_id, row.level, row.arm, row.repeat) == (
        task.task_id,
        task.level,
        run.arm,
        2,
    )
    assert row.outcome is outcome
    assert row.usable is (outcome in (EnumRunOutcome.PASS, EnumRunOutcome.FAIL_WRONG))
    assert row.floor_passed is (outcome is EnumRunOutcome.PASS)
    assert row.artifact_produced is bool(content.strip())
    assert row.artifact_chars == len(content)
    assert row.truncated is (outcome is EnumRunOutcome.TRUNCATED)
    assert run.truncated == row.truncated
    assert (
        row.total_cost_usd,
        row.decomposition_tax_usd,
        row.total_latency_ms,
        row.n_slices,
    ) == (0.25, 0.05, 12, 1)
    assert row.error == error
    assert row.blocked == run.blocked
    assert row.floor_detail
    if not content.strip():
        assert row.floor_detail == f"{outcome.value}: {error or 'no artifact'}"
    assert f"[rep 2] -> {outcome.value} usable={row.usable}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("outcomes", "scored", "passes", "excluded", "all_pass"),
    [
        (
            [EnumRunOutcome.PASS, EnumRunOutcome.BLOCKED, EnumRunOutcome.TRUNCATED],
            1,
            1,
            2,
            True,
        ),
        ([EnumRunOutcome.PASS, EnumRunOutcome.FAIL_WRONG], 2, 1, 0, False),
        (
            [
                EnumRunOutcome.BLOCKED,
                EnumRunOutcome.TRUNCATED,
                EnumRunOutcome.NO_ARTIFACT,
            ],
            0,
            0,
            2,
            False,
        ),
        ([], 0, 0, 0, False),
    ],
    ids=["excluded-do-not-deflate", "wrong-fails-reliability", "no-signal", "empty"],
)
def test_aggregate_cells(
    task: SweTask,
    outcomes: list[EnumRunOutcome],
    scored: int,
    passes: int,
    excluded: int,
    all_pass: bool,
) -> None:
    arm = EnumArm.A_MONOLITH_FRONTIER
    rows = [
        GradedRow(
            task_id=task.task_id,
            level=task.level,
            arm=arm,
            decomposition=arm.decomposition,
            routing=arm.routing,
            repeat=rep,
            outcome=outcome,
            artifact_produced=False,
            artifact_chars=0,
            floor_passed=False,
            floor_detail="fixture",
            usable=False,
            total_cost_usd=rep + 1,
        )
        for rep, outcome in enumerate(outcomes)
    ]
    other_arm = EnumArm.B_MONOLITH_COST_ROUTED
    unrelated = GradedRow(
        task_id="other-task",
        level=1,
        arm=arm,
        decomposition=arm.decomposition,
        routing=arm.routing,
        outcome=EnumRunOutcome.PASS,
        artifact_produced=True,
        artifact_chars=1,
        floor_passed=True,
        floor_detail="fixture",
        usable=True,
        total_cost_usd=100,
    )
    cell, empty = run_smoke._aggregate_cells(
        [task], [arm, other_arm], [*rows, unrelated], 3
    )
    assert (cell.task_id, cell.level, cell.arm, cell.k) == (task.task_id, 1, arm, 3)
    assert (cell.scored_repeats, cell.passes, cell.excluded_repeats) == (
        scored,
        passes,
        excluded,
    )
    assert cell.pass_hat_k is all_pass
    assert cell.pass_at_1 is (passes > 0)
    assert cell.outcomes == outcomes
    assert cell.mean_cost_usd == ((len(rows) + 1) / 2 if rows else 0)
    assert empty.arm is other_arm
    assert (
        empty.scored_repeats,
        empty.passes,
        empty.excluded_repeats,
        empty.mean_cost_usd,
    ) == (0, 0, 0, 0)
    assert not empty.pass_hat_k
    assert not empty.pass_at_1


@pytest.mark.parametrize("mode", ["pass", "blocked", "raises", "filtered"])
def test_main_offline(
    mode: str,
    task: SweTask,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"tasks": [task.model_dump(mode="json")]}))
    out_dir = tmp_path / "output"
    resolve = Mock(
        side_effect=lambda tier, _config: (
            _URL,
            f"{tier.value}-model",
            "fixture",
            {},
            tier.value,
        )
    )
    monkeypatch.setattr(run_smoke, "resolve_tier", resolve)
    fake_chat = Mock(
        return_value=_call(
            content="" if mode == "blocked" else _CODE,
            status=429 if mode == "blocked" else 200,
            error="rate limited" if mode == "blocked" else "",
        )
    )
    if mode == "raises":
        fake_chat.side_effect = RuntimeError("fixture failure")
    monkeypatch.setattr(arm_runner, "chat", fake_chat)
    argv = [
        "--corpus",
        str(corpus),
        "--out-dir",
        str(out_dir),
        "--arms",
        EnumArm.A_MONOLITH_FRONTIER.value,
        "--k",
        "0",
        "--max-retries",
        "1",
        "--max-tokens",
        "512",
    ]
    if mode == "filtered":
        argv += ["--task", "absent-task"]
    assert run_smoke.main(argv) == 0
    assert [call.args[0] for call in resolve.call_args_list] == list(EnumRouting)
    if mode == "filtered":
        fake_chat.assert_not_called()
    else:
        assert fake_chat.call_count == 1
        config = fake_chat.call_args.kwargs["runtime_config"]
        assert isinstance(config, ModelSweDiscriminatorRuntimeConfig)
        assert (config.max_retries, config.max_tokens) == (1, 512)
    report = SmokeReport.model_validate_json(
        (out_dir / "smoke_report.json").read_text()
    )
    runs = json.loads((out_dir / "arm_runs.json").read_text())
    rows = json.loads((out_dir / "graded_rows.json").read_text())
    assert rows == [row.model_dump(mode="json") for row in report.rows]
    assert report.k == 1
    assert report.n_arms == 1
    assert report.n_tasks == (0 if mode == "filtered" else 1)
    assert (
        report.total_rows == len(runs) == len(rows) == (0 if mode == "filtered" else 1)
    )
    assert report.usable_rows == (1 if mode == "pass" else 0)
    assert report.blocked_rows == (1 if mode in ("blocked", "raises") else 0)
    assert report.truncated_rows == 0
    assert report.zero_usable_rows is (mode != "pass")
    assert report.frontier_model == "frontier-model"
    assert report.cost_routed_model == "cost_routed-model"
    output = capsys.readouterr().out
    assert "frontier=frontier-model cost_routed=cost_routed-model k=1" in output
    assert "=== PHASE 1: RUN" in output
    assert "=== PHASE 2: GRADE" in output
    assert f"{report.usable_rows}/{report.total_rows} usable rows" in output
    assert f"artifacts written under {out_dir}" in output
    if mode == "pass":
        assert "pipeline emits rows" in output
        assert "pass^1=Y (1/1 scored, 0 excl)" in output
    else:
        assert "ZERO — collapse" in output
        if mode != "filtered":
            assert "no-signal" in output
    if mode == "raises":
        assert (
            "uncaught run_arm error: RuntimeError('fixture failure')"
            in rows[0]["error"]
        )


def test_main_argument_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        run_smoke.main(["--out-dir", str(tmp_path), "--decomposer-tier", "invalid"])
    assert exc.value.code == 2
    assert "invalid choice: 'invalid'" in capsys.readouterr().err


@pytest.mark.parametrize("option", ["--api-key", "--endpoint-url", "--model-name"])
def test_main_repeatable_mapping_currently_rejects_list_input(
    option: str, tmp_path: Path
) -> None:
    # argparse uses action=append, while _parse_key_values accepts a string.
    # Characterize the existing CLI failure without changing production code.
    with pytest.raises(AttributeError, match="'list' object has no attribute 'split'"):
        run_smoke.main(["--out-dir", str(tmp_path), option, "TEST=value"])
    assert not (tmp_path / "arm_runs.json").exists()


def test_main_fails_fast_on_unresolved_tier(
    task: SweTask, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_smoke, "load_corpus", lambda _path: [task])
    monkeypatch.setattr(
        run_smoke, "resolve_tier", Mock(side_effect=RuntimeError("missing endpoint"))
    )
    runner = Mock()
    monkeypatch.setattr(arm_runner, "chat", runner)
    with pytest.raises(RuntimeError, match="missing endpoint"):
        run_smoke.main(["--out-dir", str(tmp_path)])
    runner.assert_not_called()
    assert not (tmp_path / "arm_runs.json").exists()
