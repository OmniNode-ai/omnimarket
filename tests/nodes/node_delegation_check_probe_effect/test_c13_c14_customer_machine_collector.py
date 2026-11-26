# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The C13 and C14 collector: one delegation through the customer path in a clean tool environment, measured (OMN-26043).

C13 and C14 read PRECONDITION-ABSENT because the clean-install machine raised ``CollectorAbsentError`` for the delegation run
and the routing walk. These tests run the collector's pieces over fakes (a fake runner standing in for ``uv`` and the
installed interpreter, a canned in-machine record) and the in-machine driver's pure parts for real: the egress parser with its
control, the token counter, the run-directory reader, and the routing walk over this repository's own tier files.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from _gateway_fake import FakeClock, FakeGateway

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelChainResolution,
    ModelResponderReading,
)
from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckResponder,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers import (
    clean_delegation_driver as driver,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_clean_install_machine import (
    CleanInstallCustomerMachine,
    CommandResult,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    InMemoryCheckPublisher,
    InMemoryResponderReader,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.gateway_checks import (
    GatewayRunSettings,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    HandlerDelegationCheckProbe,
    contract_topics,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    EnumCheckVerdict,
    ModelDelegationCheckRequest,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    CollectorAbsentError,
)

BASE = "http://127.0.0.12:8000"
MODEL = "qwen3.8-27b"
INSTALL = "uv tool install --python 3.12 --with 'omnibase-infra>=0.38.67' --with 'omnimarket>=0.4.205' 'omnibase-core>=0.46.8'"
GUIDE = f'# Quickstart\n\n```bash\n{INSTALL}\n```\n\n```bash\nonex delegate "say hello"\n```\n'
RESPONDER = ModelChainResolution(
    tier="local", base_url=BASE, served_model=MODEL, window=131072
)


# ----- the in-machine driver's pure parts ---------------------------------------------------------------------------


def test_the_egress_parser_separates_loopback_and_the_responder_from_everything_else() -> (
    None
):
    log = ["127.0.0.1:5000", "127.0.0.12:8000", "::1:80", "203.0.113.7:443"]
    external, model_connects = driver.classify_connects(log, BASE)
    assert external == ["203.0.113.7:443"]
    assert model_connects == 1


def test_the_egress_control_is_classified_external_by_the_same_parser() -> None:
    external, _ = driver.classify_connects([driver.CONTROL_TARGET], BASE)
    assert external == [driver.CONTROL_TARGET]


def test_the_token_counter_sums_the_generation_samples_and_ignores_the_rest() -> None:
    text = (
        "# HELP x\n"
        'vllm:generation_tokens_total{model_name="m"} 120.0\n'
        'vllm:prompt_tokens_total{model_name="m"} 900.0\n'
        "llamacpp:tokens_predicted_total 30\n"
        "process_cpu_seconds_total 4\n"
    )
    assert driver.generated_tokens(text) == 150.0
    assert driver.generated_tokens("") == 0.0


def test_a_counter_published_a_moment_after_the_answer_is_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    texts = iter(["", "llamacpp:tokens_predicted_total 5\n"])
    monkeypatch.setattr(driver, "_get", lambda _url: (200, next(texts)))
    monkeypatch.setattr(driver.time, "sleep", lambda _s: None)
    assert driver.counter_after(BASE, 0.0) == 5.0


def test_a_counter_that_never_moves_is_returned_unmoved_after_the_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(driver, "_get", lambda _url: (200, ""))
    monkeypatch.setattr(driver.time, "sleep", lambda _s: None)
    assert driver.counter_after(BASE, 0.0, wait_s=0.0) == 0.0


def _write_run(root: Path, **over: Any) -> Path:
    run_dir = root / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    receipt: dict[str, Any] = {
        "run_id": "run-1",
        "correlation_id": "corr-1",
        "status": "success",
        "routing_tier": "local",
        "model": MODEL,
        "endpoint": f"{BASE}/v1/chat/completions",
        "receipt": {
            "correlation_id": "corr-1",
            "result": {
                "attempts": [{"tier": "local", "acceptance_decision": "accept"}],
                "escalation_count": 0,
                "metrics": {"cost_usd": 0.0},
            },
            "runtime_identity": {
                "packages": {
                    name: {"name": name, "source": "registry"}
                    for name in ("omnibase_core", "omnibase_infra", "omnimarket")
                }
            },
        },
    }
    receipt.update(over)
    (run_dir / "receipt.json").write_text(json.dumps(receipt))
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": "run-1", "correlation_id": "corr-1"})
    )
    (run_dir / "result.txt").write_text("ready")
    return run_dir


def test_a_run_directory_with_three_agreeing_files_reads_local_and_free(
    tmp_path: Path,
) -> None:
    read = driver.read_run(_write_run(tmp_path))
    assert read["three_files_agree"] is True
    assert read["receipt_tier"] == "local"
    assert read["receipt_model"] == MODEL
    assert read["attempt_tiers"] == ["local"]
    assert read["accepted_last"] is True
    assert read["escalated"] is False
    assert read["cost_usd"] == 0.0
    assert read["no_checkout"] is True


def test_a_checkout_package_or_a_disagreeing_run_id_reads_red(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path)
    receipt = json.loads((run_dir / "receipt.json").read_text())
    receipt["receipt"]["runtime_identity"]["packages"]["omnimarket"]["source"] = "vcs"
    (run_dir / "receipt.json").write_text(json.dumps(receipt))
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": "other", "correlation_id": "corr-1"})
    )
    read = driver.read_run(run_dir)
    assert read["no_checkout"] is False
    assert read["three_files_agree"] is False


def test_the_routing_walk_over_this_repositorys_tiers_binds_and_both_controls_hold(
    tmp_path: Path,
) -> None:
    walk = driver.walk_routing(scratch=tmp_path)
    assert walk["unbindable"] == []
    assert walk["undeclared_control_classified_deleted"] is True
    assert walk["closed_port_control_classified_unreachable"] is True


def test_the_routing_walk_names_an_unbindable_tier_backend(tmp_path: Path) -> None:
    import yaml

    from omnimarket.validators.routing_tier_backend_bindability import (
        ROUTING_TIERS_PATH,
    )

    tiers = yaml.safe_load(ROUTING_TIERS_PATH.read_text())
    tiers["tiers"][0]["models"].append({"backend_id": "no-such-backend"})
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(tiers))
    walk = driver.walk_routing(scratch=tmp_path, tiers_path=bad)
    assert any("no-such-backend" in u for u in walk["unbindable"])


def test_a_judge_that_answers_models_without_a_credential_is_private_and_unmetered() -> (
    None
):
    judge = driver.read_judge(BASE, MODEL, (200, {"data": [{"id": MODEL}]}))
    assert judge == {
        "judge_endpoint_private": True,
        "judge_needs_credential": False,
        "judge_serves_model": True,
    }
    refused = driver.read_judge(BASE, MODEL, (401, None))
    assert refused["judge_needs_credential"] is True
    public = driver.read_judge(
        "http://8.8.8.8:8000", MODEL, (200, {"data": [{"id": MODEL}]})
    )
    assert public["judge_endpoint_private"] is False


# ----- the machine ---------------------------------------------------------------------------------------------------


class _Sources:
    def guide(self) -> tuple[str, str]:
        return "guides/onex-plugin-quickstart.md", GUIDE

    def registry(self) -> Mapping[str, Any]:
        return {"patterns": []}


def _record(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "rc": 0,
        "no_checkout": True,
        "three_files_agree": True,
        "receipt_tier": "local",
        "receipt_model": MODEL,
        "receipt_endpoint": f"{BASE}/v1/chat/completions",
        "attempt_tiers": ["local"],
        "accepted_last": True,
        "escalated": False,
        "cost_usd": 0.0,
        "egress_recorded": True,
        "egress_external": [],
        "model_connects": 2,
        "token_counter_moved": True,
        "control_external_classified": True,
        "entry_path": "/clean/bin/onex",
        "entry_is_install_own": True,
        "versions": {"omnimarket": "0.4.325"},
        "routing": {
            "unbindable": [],
            "undeclared_control_classified_deleted": True,
            "closed_port_control_classified_unreachable": True,
            "judge_endpoint_private": True,
            "judge_needs_credential": False,
            "judge_serves_model": True,
        },
    }
    base.update(over)
    return base


class _Runner:
    def __init__(self, record: dict[str, Any], *, install_rc: int = 0) -> None:
        self._record = record
        self._install_rc = install_rc
        self.calls: list[tuple[list[str], dict[str, str], str | None]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        timeout_s: float,
        stdin: str | None = None,
    ) -> CommandResult:
        self.calls.append((list(argv), dict(env), stdin))
        if argv[0] == "uv":
            return CommandResult(self._install_rc, "", "resolution failed")
        return CommandResult(0, json.dumps(self._record), "")


def _uv(tmp_path: Path) -> str:
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n")
    uv.chmod(0o755)
    return str(uv)


def _machine(tmp_path: Path, runner: _Runner) -> CleanInstallCustomerMachine:
    return CleanInstallCustomerMachine(
        sources=_Sources(), runner=runner, uv=_uv(tmp_path), host_env={}
    )


_CLOCK = FakeClock()


def _handler(machine: CleanInstallCustomerMachine) -> HandlerDelegationCheckProbe:
    return HandlerDelegationCheckProbe(
        responders=(
            ModelDelegationCheckResponder(tier="local", base_url=BASE),
            ModelDelegationCheckResponder(tier="openrouter"),
            ModelDelegationCheckResponder(tier="claude_api"),
        ),
        reader=InMemoryResponderReader(
            {
                BASE: ModelResponderReading(
                    base_url=BASE, answered=True, served_model=MODEL, n_ctx=131072
                )
            }
        ),
        machine=machine,
        publisher=InMemoryCheckPublisher(),
        topic=contract_topics()[0],
        gateway=FakeGateway(),
        gateway_settings=GatewayRunSettings(run_budget_s=5.0, poll_interval_s=0.0),
        sleep=_CLOCK.sleep,
        monotonic=_CLOCK.monotonic,
    )


def _run(check: str, handler: HandlerDelegationCheckProbe) -> Any:
    return handler.handle(
        ModelDelegationCheckRequest(correlation_id="t", check_id=check)  # type: ignore[arg-type]
    )


def _machine_clauses(result: Any) -> list[Any]:
    return [c for c in result.clauses if not c.name.startswith("gateway_")]


@pytest.mark.parametrize("check", ["C13", "C14"])
def test_a_good_customer_run_grades_every_machine_clause_green(
    tmp_path: Path, check: str
) -> None:
    result = _run(check, _handler(_machine(tmp_path, _Runner(_record()))))
    assert not [p for p in result.preconditions if not p.present]
    machine = _machine_clauses(result)
    assert machine
    assert all(c.ok for c in machine), [c for c in machine if not c.ok]


def test_the_delegation_runs_in_the_clean_environment_through_the_responder_the_chain_chose(
    tmp_path: Path,
) -> None:
    runner = _Runner(_record())
    _machine(tmp_path, runner).run_delegation("C13", RESPONDER)
    install, drive = runner.calls
    assert install[0][0] == "uv"
    argv, env, stdin = drive
    assert BASE in argv
    assert MODEL in argv
    assert stdin is not None
    assert "def drive" in stdin
    assert "OMNI" + "_HOME" not in env


def test_the_routing_collection_comes_from_the_same_clean_machine_record(
    tmp_path: Path,
) -> None:
    routing = _machine(tmp_path, _Runner(_record())).collect_routing(RESPONDER)
    assert routing.unbindable == ()
    assert routing.judge_serves_model


def test_known_bad_a_receipt_naming_another_model_reads_c13_fail(
    tmp_path: Path,
) -> None:
    runner = _Runner(
        _record(receipt_model="claude-sonnet-5-5", receipt_tier="claude_api")
    )
    result = _run("C13", _handler(_machine(tmp_path, runner)))
    assert result.verdict is EnumCheckVerdict.FAIL
    assert "local_model" in {c.name for c in result.clauses if not c.ok}


def test_known_bad_an_external_connect_reads_c13_fail(tmp_path: Path) -> None:
    runner = _Runner(_record(egress_external=["203.0.113.7:443"]))
    result = _run("C13", _handler(_machine(tmp_path, runner)))
    assert result.verdict is EnumCheckVerdict.FAIL
    assert "zero_provider" in {c.name for c in result.clauses if not c.ok}


def test_known_bad_an_unbindable_tier_reads_c14_fail(tmp_path: Path) -> None:
    routing = _record()["routing"] | {"unbindable": ["undeclared:ghost"]}
    result = _run(
        "C14", _handler(_machine(tmp_path, _Runner(_record(routing=routing))))
    )
    assert result.verdict is EnumCheckVerdict.FAIL
    assert "row2_no_unbindable_tier" in {c.name for c in result.clauses if not c.ok}


def test_an_install_that_fails_reads_fail_and_the_driver_never_runs(
    tmp_path: Path,
) -> None:
    runner = _Runner(_record(), install_rc=1)
    result = _run("C13", _handler(_machine(tmp_path, runner)))
    assert result.verdict is EnumCheckVerdict.FAIL
    assert len(runner.calls) == 1


def test_a_driver_that_prints_no_record_is_a_collector_absent_not_a_pass(
    tmp_path: Path,
) -> None:
    class _Mute(_Runner):
        def run(self, argv: Sequence[str], **kw: Any) -> CommandResult:  # type: ignore[override]
            if argv[0] == "uv":
                return CommandResult(0, "", "")
            return CommandResult(1, "", "boom")

    with pytest.raises(CollectorAbsentError):
        _machine(tmp_path, _Mute(_record())).run_delegation("C13", RESPONDER)
