# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The customer-path container test reads every declared step, names each failure and never carries the key."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from omnimarket.nodes.node_customer_path_container_test_effect.handlers.handler_customer_path_container_test import (
    HandlerCustomerPathContainerTest,
)
from omnimarket.nodes.node_customer_path_container_test_effect.models.model_customer_path_container_test import (
    EnumCustomerPathOutcome,
    EnumStepStatus,
    ModelCustomerPathCommand,
    ModelExecResult,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_customer_path_container_test_effect"
)
KEY_VALUE = "synthetic-key-value-never-printed"
URL = "https://127.0.0.1:8443"
PROBE = "http://lab-probe.invalid:9"
RECEIPT = {
    "status": "completed",
    "terminal_model_used": "model-x",
    "provider": "provider-y",
}
SMOKE_OK = {"ok": True, "outcome": "passed", "model": "model-x"}


class FakeRuntime:
    """Answers each exec by a substring of its argv; anything unscripted exits 0 with no output."""

    def __init__(self, scripted: dict[str, ModelExecResult] | None = None) -> None:
        self.scripted = scripted or {}
        self.calls: list[tuple[str, list[str], str, str | None]] = []
        self.started: list[str] = []
        self.removed: list[str] = []
        self.copied: list[tuple[str, str]] = []
        self.start_error: str | None = None

    def start(self, image: str, ttl_s: int) -> str:
        if self.start_error:
            raise RuntimeError(self.start_error)
        self.started.append(image)
        return "c1"

    def exec(
        self,
        container: str,
        argv: list[str],
        *,
        user: str,
        stdin: str | None,
        timeout_s: float,
    ) -> ModelExecResult:
        self.calls.append((container, argv, user, stdin))
        line = " ".join(argv)
        if argv[:3] == ["ip", "-4", "route"]:
            return ModelExecResult(
                exit_code=0, stdout="default via 198.51.100.1 dev eth0"
            )
        for needle, answer in self.scripted.items():
            if needle in line:
                return answer
        if PROBE in line:
            return ModelExecResult(exit_code=7, stderr="no route")
        if "node_cloud_smoke_check_effect" in line:
            return ModelExecResult(exit_code=0, stdout=json.dumps(SMOKE_OK))
        if "receipt.json" in line:
            return ModelExecResult(exit_code=0, stdout=json.dumps(RECEIPT))
        return ModelExecResult(exit_code=0)

    def copy_in(self, container: str, source: str, destination: str) -> ModelExecResult:
        self.copied.append((source, destination))
        return ModelExecResult(exit_code=0)

    def remove(self, container: str) -> None:
        self.removed.append(container)


def _env(tmp_path: Path, mode: int = 0o600, **over: str) -> dict[str, str]:
    key = tmp_path / "key"
    key.write_text(KEY_VALUE + "\n", encoding="utf-8")
    key.chmod(mode)
    env = {
        "ONEX_API_BASE_URL": URL,
        "ONEX_API_KEY_FILE": str(key),
        "ONEX_CUSTOMER_PATH_LAB_PROBE_TARGETS": PROBE,
        "ONEX_CUSTOMER_PATH_TENANT_SLUG": "tenant-t",
        "ONEX_CUSTOMER_PATH_BLOCKED_RANGES": "range-a,range-b,range-c,range-d,range-e",
        "ONEX_CUSTOMER_PATH_UV_INSTALL_URL": "uv-url.invalid",
    }
    env.update(over)
    return env


def _run(env: dict[str, str], runtime: FakeRuntime, **command: object):
    handler = HandlerCustomerPathContainerTest(environ=env, runtime=runtime)
    request = ModelCustomerPathCommand.model_validate(
        {"source_checkout": "/checkout", **command}
    )
    return asyncio.run(handler.handle(request))


def test_healthy_run_passes_every_step_that_runs_and_reads_the_receipt(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    result = _run(_env(tmp_path), runtime)
    assert result.ok is True
    assert result.outcome is EnumCustomerPathOutcome.PASSED_STEPS_NOT_RUN
    assert (result.receipt_status, result.receipt_model, result.receipt_provider) == (
        "completed",
        "model-x",
        "provider-y",
    )
    assert result.receipt_tests_run == "the receipt has no tests field"
    by_id = {s.step_id: s for s in result.steps}
    assert by_id["s10_register_provider_key"].status is EnumStepStatus.NOT_RUN
    assert all(
        s.status is EnumStepStatus.PASSED
        for s in result.steps
        if s.step_id != "s10_register_provider_key"
    )
    assert runtime.removed == ["c1"]
    assert [(c[0], c[1].rsplit("/", 1)[-1]) for c in runtime.copied] == [
        ("/checkout", "omnimarket-src")
    ]
    assert len(result.guide_gaps) >= 3


def test_lab_ranges_are_blackholed_and_only_the_gateway_is_allowed(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    _run(_env(tmp_path), runtime)
    routes = [c[1] for c in runtime.calls if c[1][:3] == ["ip", "route", "add"]]
    assert [r[3] for r in routes if r[3] == "blackhole"] == ["blackhole"] * 5
    assert routes[-1] == ["ip", "route", "add", "127.0.0.1/32", "via", "198.51.100.1"]
    users = {c[2] for c in runtime.calls if c[1][0] == "useradd"}
    assert users == {"root"}
    assert all(c[2] == "customer" for c in runtime.calls if c[1][0] == "curl")


def test_the_key_goes_only_on_stdin_and_never_reaches_the_result(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    result = _run(_env(tmp_path), runtime)
    assert all(KEY_VALUE not in " ".join(argv) for _, argv, _, _ in runtime.calls)
    assert [c[3] for c in runtime.calls if c[3] == KEY_VALUE]
    login = [c for c in runtime.calls if "onex cloud login" in " ".join(c[1])]
    assert [c[3] for c in login] == [KEY_VALUE]
    assert KEY_VALUE not in result.model_dump_json()


def test_a_key_in_command_output_is_scrubbed(tmp_path: Path) -> None:
    runtime = FakeRuntime(
        {"onex cloud status": ModelExecResult(exit_code=0, stdout=f"key {KEY_VALUE}")}
    )
    result = _run(_env(tmp_path), runtime)
    assert KEY_VALUE not in result.model_dump_json()


def test_no_settings_fails_before_any_container_starts() -> None:
    runtime = FakeRuntime()
    result = _run({}, runtime)
    assert result.ok is False
    assert result.outcome is EnumCustomerPathOutcome.SETTINGS_ABSENT
    for name in (
        "ONEX_API_BASE_URL",
        "ONEX_API_KEY_FILE",
        "ONEX_CUSTOMER_PATH_LAB_PROBE_TARGETS",
    ):
        assert name in result.detail
    assert runtime.started == []


def test_a_group_readable_key_file_is_refused(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    result = _run(_env(tmp_path, mode=0o644), runtime)
    assert result.outcome is EnumCustomerPathOutcome.KEY_FILE_REFUSED
    assert runtime.started == []


def test_a_lab_address_that_answers_fails_the_run_and_stops_it(tmp_path: Path) -> None:
    runtime = FakeRuntime({"lab-probe": ModelExecResult(exit_code=0)})
    result = _run(_env(tmp_path), runtime)
    assert result.ok is False
    assert result.outcome is EnumCustomerPathOutcome.STEP_FAILED
    assert result.detail.startswith("s04_lab_address_unreachable")
    by_id = {s.step_id: s for s in result.steps}
    assert by_id["s05_gateway_reachable"].status is EnumStepStatus.NOT_RUN
    assert not [
        c for c in runtime.calls if "node_cloud_smoke_check_effect" in " ".join(c[1])
    ]
    assert runtime.removed == ["c1"]


def test_a_failed_install_names_its_step(tmp_path: Path) -> None:
    runtime = FakeRuntime(
        {
            "uv tool install --python 3.12 --with 'omnibase-infra>=0.38.67' --with 'omnimarket": ModelExecResult(
                exit_code=1, stderr="no wheel"
            )
        }
    )
    result = _run(_env(tmp_path), runtime)
    assert result.outcome is EnumCustomerPathOutcome.STEP_FAILED
    assert result.detail.startswith("s07_install_onex")


def test_a_failed_smoke_check_fails_the_run_with_its_outcome(tmp_path: Path) -> None:
    bad = {"ok": False, "outcome": "gateway_unavailable"}
    runtime = FakeRuntime(
        {
            "node_cloud_smoke_check_effect": ModelExecResult(
                exit_code=0, stdout=json.dumps(bad)
            )
        }
    )
    result = _run(_env(tmp_path), runtime)
    assert result.outcome is EnumCustomerPathOutcome.STEP_FAILED
    assert "gateway_unavailable" in result.detail


def test_a_receipt_with_no_model_fails_the_last_step(tmp_path: Path) -> None:
    runtime = FakeRuntime(
        {
            "receipt.json": ModelExecResult(
                exit_code=0, stdout=json.dumps({"status": "completed"})
            )
        }
    )
    result = _run(_env(tmp_path), runtime)
    assert result.outcome is EnumCustomerPathOutcome.STEP_FAILED
    assert result.detail.startswith("s17_read_receipt")


def test_a_container_that_will_not_start_is_named(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    runtime.start_error = "docker run failed: no such image"
    result = _run(_env(tmp_path), runtime)
    assert result.outcome is EnumCustomerPathOutcome.CONTAINER_START_FAILED
    assert "no such image" in result.detail


def test_no_checkout_fails_the_copy_step_and_names_the_gap(tmp_path: Path) -> None:
    handler = HandlerCustomerPathContainerTest(
        environ=_env(tmp_path), runtime=FakeRuntime()
    )
    result = asyncio.run(handler.handle(ModelCustomerPathCommand()))
    assert result.outcome is EnumCustomerPathOutcome.STEP_FAILED
    assert result.detail.startswith("s11_copy_unreleased_node")
    assert any("not yet released" in g for g in result.guide_gaps)


def test_the_declared_steps_follow_the_guide_in_order() -> None:
    import yaml

    steps = yaml.safe_load((NODE / "contract.yaml").read_text())["customer_path"][
        "steps"
    ]
    ids = [s["id"] for s in steps]
    assert ids == sorted(ids)
    assert ids.index("s02_block_lab_network") < ids.index("s05_gateway_reachable")
    assert (
        ids.index("s06_install_uv")
        < ids.index("s07_install_onex")
        < ids.index("s15_smoke_delegation")
    )


def test_source_holds_no_host_or_key_literal() -> None:
    pattern = re.compile(
        r"https?://[A-Za-z0-9]|[0-9]{1,3}[.][0-9]{1,3}[.][0-9]{1,3}[.][0-9]{1,3}|onxk_"
    )
    hits = [
        str(p)
        for p in NODE.rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert hits == []


PROVIDER_VALUE = "synthetic-provider-key-never-printed"


def _provider_file(tmp_path: Path, mode: int = 0o600) -> Path:
    path = tmp_path / "provider-key"
    path.write_text(PROVIDER_VALUE + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_own_key_step_registers_the_provider_key_on_stdin_when_a_file_is_named(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    env = _env(
        tmp_path,
        ONEX_CUSTOMER_PATH_PROVIDER_KEY_FILE=str(_provider_file(tmp_path)),
    )
    result = _run(env, runtime)
    by_id = {s.step_id: s for s in result.steps}
    assert by_id["s10_register_provider_key"].status is EnumStepStatus.PASSED
    assert result.outcome is EnumCustomerPathOutcome.PASSED
    calls = [c for c in runtime.calls if "register-tenant-key" in " ".join(c[1])]
    assert len(calls) == 1
    assert "--tenant 'tenant-t'" in " ".join(
        calls[0][1]
    ) or "--tenant tenant-t" in " ".join(calls[0][1])
    assert calls[0][2] == "customer"
    assert calls[0][3] == PROVIDER_VALUE
    assert PROVIDER_VALUE not in " ".join(" ".join(c[1]) for c in runtime.calls)


def test_own_key_step_is_not_run_and_says_why_when_no_file_is_named(
    tmp_path: Path,
) -> None:
    result = _run(_env(tmp_path), FakeRuntime())
    step = {s.step_id: s for s in result.steps}["s10_register_provider_key"]
    assert step.status is EnumStepStatus.NOT_RUN
    assert "ONEX_CUSTOMER_PATH_PROVIDER_KEY_FILE" in step.detail


def test_a_provider_key_file_that_is_not_owner_only_is_refused(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    env = _env(
        tmp_path,
        ONEX_CUSTOMER_PATH_PROVIDER_KEY_FILE=str(_provider_file(tmp_path, 0o644)),
    )
    result = _run(env, runtime)
    assert result.outcome is EnumCustomerPathOutcome.KEY_FILE_REFUSED
    assert runtime.started == []


def test_the_provider_key_is_scrubbed_from_output_and_a_refusal_names_the_step(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime(
        {
            "register-tenant-key": ModelExecResult(
                exit_code=1, stderr=f"refused {PROVIDER_VALUE}"
            )
        }
    )
    env = _env(
        tmp_path,
        ONEX_CUSTOMER_PATH_PROVIDER_KEY_FILE=str(_provider_file(tmp_path)),
    )
    result = _run(env, runtime)
    assert result.outcome is EnumCustomerPathOutcome.STEP_FAILED
    assert result.detail.startswith("s10_register_provider_key")
    assert PROVIDER_VALUE not in result.model_dump_json()
