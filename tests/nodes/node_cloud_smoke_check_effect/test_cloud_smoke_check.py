# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The cloud smoke check runs one delegation through a gateway from settings and reads the answering model off the receipt."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_cloud_smoke_check_effect.handlers.handler_cloud_smoke_check import (
    HandlerCloudSmokeCheck,
)
from omnimarket.nodes.node_cloud_smoke_check_effect.models.model_cloud_smoke_check import (
    EnumCloudSmokeOutcome,
    ModelCloudSmokeCommand,
    ModelGatewayAnswer,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_cloud_smoke_check_effect"
)
KEY_VALUE = "synthetic-key-value-never-printed"
URL = "https://gateway.invalid:8443"


class FakeGateway:
    """A scripted gateway: path-and-method keyed answers, every request recorded."""

    def __init__(
        self, answers: Mapping[tuple[str, str], list[ModelGatewayAnswer]]
    ) -> None:
        self._answers = {k: list(v) for k, v in answers.items()}
        self.requests: list[tuple[str, str, str, dict[str, Any] | None]] = []

    def call(
        self,
        base_url: str,
        method: str,
        path: str,
        *,
        api_key: str,
        body: dict[str, Any] | None,
    ) -> ModelGatewayAnswer:
        self.requests.append((base_url, method, path, body))
        route, _, query = path.partition("?")
        # The live gateway answers 422 to a receipt read that names no runner identity (seen against the lab dev gateway).
        if route.endswith("/receipt") and not query.startswith("runner_identity="):
            return ModelGatewayAnswer(status=422, body={})
        queue = self._answers.get((method, route))
        if not queue:
            return ModelGatewayAnswer(status=404, body={})
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _key_file(tmp_path: Path, mode: int = 0o600) -> Path:
    path = tmp_path / "key"
    path.write_text(KEY_VALUE + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _env(tmp_path: Path, **over: str) -> dict[str, str]:
    env = {"ONEX_API_BASE_URL": URL, "ONEX_API_KEY_FILE": str(_key_file(tmp_path))}
    env.update(over)
    return env


def _healthy(model: str | None = "model-x") -> FakeGateway:
    receipt: dict[str, Any] = {"status": "completed", "route": "r", "provider": "p"}
    if model is not None:
        receipt["terminal_model_used"] = model
    return FakeGateway(
        {
            ("POST", "/v1/workflows"): [
                ModelGatewayAnswer(status=202, body={"workflow_id": "wf-1"})
            ],
            ("GET", "/v1/workflows/wf-1/status"): [
                ModelGatewayAnswer(status=200, body={"status": "completed"})
            ],
            ("GET", "/v1/workflows/wf-1/receipt"): [
                ModelGatewayAnswer(status=200, body=receipt)
            ],
        }
    )


def _run(env: Mapping[str, str], gateway: FakeGateway) -> Any:
    handler = HandlerCloudSmokeCheck(
        environ=env, transport=gateway, sleep=lambda _s: None
    )
    return asyncio.run(handler.handle(ModelCloudSmokeCommand()))


def test_passes_and_names_the_answering_model(tmp_path: Path) -> None:
    gateway = _healthy("model-x")
    result = _run(_env(tmp_path), gateway)
    assert result.ok is True
    assert result.outcome is EnumCloudSmokeOutcome.PASSED
    assert result.model == "model-x"
    assert result.workflow_id == "wf-1"
    assert all(req[0] == URL for req in gateway.requests)


def test_no_gateway_url_is_a_named_failure_before_any_request(tmp_path: Path) -> None:
    gateway = _healthy()
    env = _env(tmp_path)
    del env["ONEX_API_BASE_URL"]
    result = _run(env, gateway)
    assert result.ok is False
    assert result.outcome is EnumCloudSmokeOutcome.SETTINGS_ABSENT
    assert "ONEX_API_BASE_URL" in result.detail
    assert gateway.requests == []


def test_no_settings_at_all_is_a_named_failure(tmp_path: Path) -> None:
    gateway = _healthy()
    result = _run({}, gateway)
    assert result.outcome is EnumCloudSmokeOutcome.SETTINGS_ABSENT
    assert "ONEX_API_KEY_FILE" in result.detail
    assert "ONEX_API_BASE_URL" in result.detail
    assert gateway.requests == []


def test_a_key_file_readable_by_others_is_refused(tmp_path: Path) -> None:
    gateway = _healthy()
    env = _env(tmp_path)
    Path(env["ONEX_API_KEY_FILE"]).chmod(0o644)
    result = _run(env, gateway)
    assert result.outcome is EnumCloudSmokeOutcome.KEY_FILE_REFUSED
    assert gateway.requests == []


def test_a_missing_key_file_is_refused(tmp_path: Path) -> None:
    result = _run(
        _env(tmp_path, ONEX_API_KEY_FILE=str(tmp_path / "absent")), _healthy()
    )
    assert result.outcome is EnumCloudSmokeOutcome.KEY_FILE_REFUSED


def test_gateway_503_is_named_unavailable(tmp_path: Path) -> None:
    gateway = FakeGateway(
        {
            ("POST", "/v1/workflows"): [
                ModelGatewayAnswer(status=503, body={"detail": "unavailable"})
            ]
        }
    )
    result = _run(_env(tmp_path), gateway)
    assert result.outcome is EnumCloudSmokeOutcome.GATEWAY_UNAVAILABLE
    assert "503" in result.detail


def test_no_answer_from_the_gateway_is_named_unreachable(tmp_path: Path) -> None:
    gateway = FakeGateway(
        {("POST", "/v1/workflows"): [ModelGatewayAnswer(status=None, error="URLError")]}
    )
    result = _run(_env(tmp_path), gateway)
    assert result.outcome is EnumCloudSmokeOutcome.GATEWAY_UNREACHABLE
    assert "URLError" in result.detail


def test_401_is_named_auth_refused(tmp_path: Path) -> None:
    gateway = FakeGateway(
        {("POST", "/v1/workflows"): [ModelGatewayAnswer(status=401, body={})]}
    )
    assert _run(_env(tmp_path), gateway).outcome is EnumCloudSmokeOutcome.AUTH_REFUSED


def test_a_receipt_with_no_model_fails_named(tmp_path: Path) -> None:
    result = _run(_env(tmp_path), _healthy(model=None))
    assert result.ok is False
    assert result.outcome is EnumCloudSmokeOutcome.RECEIPT_NAMES_NO_MODEL


def test_a_failed_run_fails_named_with_its_code(tmp_path: Path) -> None:
    gateway = FakeGateway(
        {
            ("POST", "/v1/workflows"): [
                ModelGatewayAnswer(status=202, body={"workflow_id": "wf-1"})
            ],
            ("GET", "/v1/workflows/wf-1/status"): [
                ModelGatewayAnswer(
                    status=200,
                    body={
                        "status": "failed",
                        "terminal_failure_code": "tenant.key.absent",
                    },
                )
            ],
        }
    )
    result = _run(_env(tmp_path), gateway)
    assert result.outcome is EnumCloudSmokeOutcome.RUN_FAILED
    assert "tenant.key.absent" in result.detail


def test_a_run_that_never_ends_times_out(tmp_path: Path) -> None:
    gateway = FakeGateway(
        {
            ("POST", "/v1/workflows"): [
                ModelGatewayAnswer(status=202, body={"workflow_id": "wf-1"})
            ],
            ("GET", "/v1/workflows/wf-1/status"): [
                ModelGatewayAnswer(status=200, body={"status": "running"})
            ],
        }
    )
    clock = iter(range(0, 10_000, 100))
    handler = HandlerCloudSmokeCheck(
        environ=_env(tmp_path),
        transport=gateway,
        sleep=lambda _s: None,
        monotonic=lambda: float(next(clock)),
    )
    result = asyncio.run(
        handler.handle(ModelCloudSmokeCommand(run_budget_s=60, poll_interval_s=1))
    )
    assert result.outcome is EnumCloudSmokeOutcome.RUN_TIMED_OUT


def test_the_key_never_appears_in_the_result_or_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("DEBUG"):
        for gateway in (
            _healthy(),
            FakeGateway(
                {("POST", "/v1/workflows"): [ModelGatewayAnswer(status=503, body={})]}
            ),
        ):
            result = _run(_env(tmp_path), gateway)
            assert KEY_VALUE not in result.model_dump_json()
    assert KEY_VALUE not in caplog.text


def test_no_host_or_key_literal_in_the_node_source() -> None:
    """AC3: the node source carries no URL host, IP address or key-shaped literal."""
    pattern = re.compile(r"https?://[A-Za-z0-9]|\b\d{1,3}(?:\.\d{1,3}){3}\b|onxk_")
    offenders = [
        f"{path.name}:{n}"
        for path in sorted(NODE.rglob("*.py"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []


def test_no_freestanding_module_of_the_node_makes_a_raw_http_call() -> None:
    """The imperative contract guard refuses a raw HTTP call outside the effect handler module.

    Runs the guard's own scanner over every node module that is not under ``handlers/``; the transport therefore lives
    in the handler module, as the Slack publish effect keeps its transport.
    """
    from omnibase_core.models.nodes.imperative_contract_guard.model_freestanding_scan_input import (
        ModelFreestandingScanInput,
    )
    from omnibase_core.models.nodes.imperative_contract_guard.model_guard_module_source import (
        ModelGuardModuleSource,
    )
    from omnibase_core.nodes.node_imperative_contract_guard_check_effect.handler_freestanding_imperative_io import (
        scan_freestanding_imperative_io,
    )

    offenders = []
    for path in sorted(NODE.rglob("*.py")):
        if "handlers" in path.relative_to(NODE).parts:
            continue
        result = scan_freestanding_imperative_io(
            ModelFreestandingScanInput(
                module=ModelGuardModuleSource(
                    path=path.relative_to(NODE).as_posix(),
                    source=path.read_text(encoding="utf-8"),
                ),
                repo="omnimarket",
            )
        )
        offenders += [
            f"{result.module_path}:{f.line} {f.detail}"
            for f in result.findings
            if not f.suppressed
        ]
    assert offenders == []
