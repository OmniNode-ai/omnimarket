# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The C13 collector: a real customer-local delegation on a clean tool environment, traced for egress (OMN-26043).

The failure cases come first (no uv, no strace, a public responder, a failed install, an empty egress trace, a model that never
saw the process, an external connect, an unconfigured run that answered, a metrics endpoint with no token counter), then the
green path. A scripted host stands in for ``uv``, ``strace`` and ``onex``: it writes the files and the trace each one leaves,
so the collector's own reads run on real directories.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from _gateway_fake import FakeClock, FakeGateway

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelChainResolution,
    ModelResponderReading,
)
from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckResponder,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    InMemoryCheckPublisher,
    InMemoryResponderReader,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_local_delegation_machine import (
    DIRECT_URL_DRIVER,
    OVERLAY_DRIVER,
    OVERLAY_RELATIVE_PATH,
    ROUTING_DRIVER,
    TARGET_DRIVER,
    CommandResult,
    LocalDelegationCustomerMachine,
    find_source_trees,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.gateway_checks import (
    GatewayRunSettings,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.grade_delegation_checks import (
    grade_c13,
    grade_c14,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    HandlerDelegationCheckProbe,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.observe_delegation_run import (
    ONEX_DISTRIBUTIONS,
    parse_connects,
    tokens_from_metrics,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    EnumCheckVerdict,
    ModelDelegateObservation,
    ModelDelegationCheckRequest,
    ModelRoutingCollection,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    CollectorAbsentError,
)

BASE = "http://127.0.0.12:8000"
MODEL = "qwen3.8-27b"
WINDOW = 131072
TOPIC = "onex.evt.omnimarket.delegation-check-probe-completed.v1"
ENDPOINT = f"{BASE}/v1/chat/completions"

MODEL_CONNECT = (
    '4242 connect(12, {sa_family=AF_INET, sin_port=htons(8000), sin_addr=inet_addr("127.0.0.12")}, 16) = -1 EINPROGRESS (Operation now in progress)\n'
    '4242 connect(3, {sa_family=AF_UNIX, sun_path="/run/customer/emit.sock"}, 52) = -1 ENOENT (No such file or directory)\n'
)
EXTERNAL_CONNECT = '4243 connect(5, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("104.18.2.2")}, 16) = -1 EINPROGRESS (Operation now in progress)\n'
CONTROL_CONNECT = '4244 connect(3, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("1.1.1.1")}, 16) = -1 ENETUNREACH (Network is unreachable)\n'
TARGET = {"host": "198.51.100.7", "port": 4443, "backend_id": "a-cloud-rung"}
WALK_GREEN: dict[str, object] = {
    "unbindable": [],
    "deleted_control": "deleted_backend",
    "unreachable_control": "unreachable",
    "judge": {
        "endpoint_private": True,
        "requires_credential": False,
        "serves_model": True,
    },
    "collect_error": None,
}
UNIX_ONLY = '4245 connect(3, {sa_family=AF_UNIX, sun_path="/run/customer/emit.sock"}, 52) = -1 ENOENT (No such file or directory)\n'


def _receipt(
    *,
    model: str = MODEL,
    endpoint: str = ENDPOINT,
    tier: str = "local",
    source: str = "registry",
) -> dict[str, object]:
    return {
        "correlation_id": "corr-1",
        "run_id": "run-1",
        "status": "success",
        "routing_tier": tier,
        "model": model,
        "endpoint": endpoint,
        "receipt": {
            "result": {
                "response": "A calendar app needs events.",
                "attempts": [
                    {"tier": tier, "acceptance_decision": "accept", "model_id": model}
                ],
                "escalation_count": 0,
                "metrics": {"cost_usd": 0.0},
            },
            "runtime_identity": {
                "packages": {
                    dist: {
                        "name": dist,
                        "version": "1.0.0",
                        "source": source,
                        "commit": None,
                        "import_path": None,
                    }
                    for dist in ONEX_DISTRIBUTIONS
                }
            },
        },
    }


class ScriptedHost:
    """Plays uv, strace, onex and the installed interpreter; every knob is one way a run goes wrong."""

    def __init__(
        self,
        *,
        install_rc: int = 0,
        unconfigured_rc: int = 2,
        configured_trace: str = MODEL_CONNECT,
        unconfigured_trace: str = UNIX_ONLY,
        control_trace: str = CONTROL_CONNECT,
        receipt: Mapping[str, object] | None = None,
        direct_urls: Mapping[str, object] | None = None,
        plant_checkout: bool = False,
        target: Mapping[str, object] | None = None,
        walk: Mapping[str, object] | None = None,
        walk_stdout: str | None = None,
    ) -> None:
        self.target = {
            "target": TARGET if target is None else dict(target),
            "error": None,
        }
        self.walk = dict(WALK_GREEN if walk is None else walk)
        self.walk_stdout = walk_stdout
        self.install_rc = install_rc
        self.unconfigured_rc = unconfigured_rc
        self.configured_trace = configured_trace
        self.unconfigured_trace = unconfigured_trace
        self.control_trace = control_trace
        self.receipt = receipt if receipt is not None else _receipt()
        self.direct_urls = (
            dict(direct_urls)
            if direct_urls is not None
            else dict.fromkeys(ONEX_DISTRIBUTIONS)
        )
        self.plant_checkout = plant_checkout
        self.argvs: list[list[str]] = []
        self.walked = False
        self.overlay_text: str | None = None

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        timeout_s: float,
        stdin: str | None = None,
    ) -> CommandResult:
        self.argvs.append(list(argv))
        if argv[1:3] == ["tool", "install"]:
            if self.install_rc == 0:
                bin_dir = Path(env["UV_TOOL_BIN_DIR"])
                bin_dir.mkdir(parents=True, exist_ok=True)
                onex = bin_dir / "onex"
                onex.write_text("#!/bin/sh\n")
                onex.chmod(0o755)
            if self.plant_checkout:
                tree = Path(env["HOME"]) / "src" / "omnimarket"
                tree.mkdir(parents=True)
                (tree / "pyproject.toml").write_text('[project]\nname = "omnimarket"\n')
            return CommandResult(self.install_rc, "", "")
        if argv[0].endswith("strace"):
            trace_file = Path(argv[argv.index("-o") + 1])
            inner = list(argv[argv.index("--") + 1 :])
            return self._traced(inner, trace_file, env, cwd)
        if argv[1:3] == ["-c", OVERLAY_DRIVER]:
            target, endpoint, served, window, _tokens, backends = argv[3:9]
            self.overlay_text = f"endpoint={endpoint} served={served} window={window} backends={backends}"
            Path(target).write_text(self.overlay_text)
            return CommandResult(0, "", "")
        if argv[1:3] == ["-I", "-c"] and argv[3] == TARGET_DRIVER:
            return CommandResult(0, json.dumps(self.target) + "\n", "")
        if argv[1:3] == ["-I", "-c"] and argv[3] == ROUTING_DRIVER:
            self.walked = True
            out = (
                self.walk_stdout
                if self.walk_stdout is not None
                else json.dumps(self.walk)
            )
            return CommandResult(0, out + "\n", "")
        if argv[1:2] == ["-c"] and argv[2] == DIRECT_URL_DRIVER % (ONEX_DISTRIBUTIONS,):
            return CommandResult(0, json.dumps(self.direct_urls), "")
        raise AssertionError(f"unscripted command {argv}")

    def _traced(
        self, inner: list[str], trace_file: Path, env: Mapping[str, str], cwd: Path
    ) -> CommandResult:
        if inner[1:3] == ["local", "init"]:
            trace_file.write_text("")
            return CommandResult(0, "{}", "")
        if inner[1] == "delegate":
            overlay = Path(env["HOME"]) / OVERLAY_RELATIVE_PATH
            if not overlay.exists():
                trace_file.write_text(self.unconfigured_trace)
                return CommandResult(
                    self.unconfigured_rc, "", "refused: bifrost_overrides.yaml"
                )
            trace_file.write_text(self.configured_trace)
            run_dir = Path(env["HOME"]) / ".onex_state" / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            (run_dir / "receipt.json").write_text(json.dumps(self.receipt))
            (run_dir / "result.txt").write_text("A calendar app needs events.")
            (run_dir / "run.json").write_text(
                json.dumps({"correlation_id": "corr-1", "run_id": "run-1"})
            )
            return CommandResult(
                0, json.dumps({"correlation_id": "corr-1", "run_id": "run-1"}), ""
            )
        trace_file.write_text(self.control_trace)
        return CommandResult(0, "", "")


class TokenCounter:
    """The model server's counter: it moves by ``step`` on every read."""

    def __init__(self, step: int = 40, exposition: bool = True) -> None:
        self._n = 1000
        self._step = step
        self._exposition = exposition

    def __call__(self, base_url: str) -> str:
        self._n += self._step
        if not self._exposition:
            return "# HELP something_else counter\nsomething_else_total 1\n"
        return f"# TYPE llamacpp:tokens_predicted_total counter\nllamacpp:tokens_predicted_total {self._n}\n"


def _responder(**over: object) -> ModelChainResolution:
    fields: dict[str, object] = {
        "tier": "local",
        "base_url": BASE,
        "served_model": MODEL,
        "window": WINDOW,
    }
    return ModelChainResolution.model_validate({**fields, **over})


def _machine(
    host: ScriptedHost,
    *,
    counter: TokenCounter | None = None,
    host_path: str | None = None,
    uv: str | None = "/opt/bin/uv",
    strace: str | None = "/usr/bin/strace",
) -> LocalDelegationCustomerMachine:
    return LocalDelegationCustomerMachine(
        runner=host,
        metrics_reader=counter or TokenCounter(),
        uv=uv,
        strace=strace,
        host_path=host_path,
        host_env={"PATH": host_path or "/usr/bin"},
    )


def _failed(obs: ModelDelegateObservation) -> list[str]:
    return [
        c.name
        for c in grade_c13(obs, responder_base=BASE, served_model=MODEL)
        if not c.ok
    ]


# -- an absent instrument reads PRECONDITION-ABSENT, never a pass and never a product FAIL ------------------------------


def test_no_uv_on_the_host_is_an_absent_collector(tmp_path: Path) -> None:
    machine = _machine(ScriptedHost(), uv=None, host_path=str(tmp_path))
    with pytest.raises(CollectorAbsentError) as raised:
        machine.run_delegation("C13", _responder())
    assert raised.value.name == "uv"


def test_no_strace_on_the_host_is_an_absent_collector(tmp_path: Path) -> None:
    machine = _machine(ScriptedHost(), strace=None, host_path=str(tmp_path))
    with pytest.raises(CollectorAbsentError) as raised:
        machine.run_delegation("C13", _responder())
    assert raised.value.name == "strace"


@pytest.mark.parametrize(
    "base", ["http://8.8.8.8:8000", "http://qwen.example:8000", "http://127.0.0.12"]
)
def test_a_responder_that_is_not_a_private_ip_literal_is_refused(base: str) -> None:
    host = ScriptedHost()
    with pytest.raises(CollectorAbsentError) as raised:
        _machine(host).run_delegation("C13", _responder(base_url=base))
    assert raised.value.name == "responder_address"
    assert host.argvs == []


def test_a_check_the_machine_has_no_delegation_for_is_absent_and_runs_nothing() -> None:
    host = ScriptedHost()
    with pytest.raises(CollectorAbsentError) as raised:
        _machine(host).run_delegation("C17", _responder())
    assert raised.value.name == "delegation"
    assert host.argvs == []


def test_the_surface_of_c17_is_still_absent() -> None:
    with pytest.raises(CollectorAbsentError) as raised:
        _machine(ScriptedHost()).collect_surface()
    assert raised.value.name == "clean_machine"


def test_the_routing_walk_is_absent_until_a_c14_delegation_took_it() -> None:
    with pytest.raises(CollectorAbsentError) as raised:
        _machine(ScriptedHost()).collect_routing(_responder())
    assert raised.value.name == "routing_walk"


def test_a_c13_run_takes_no_routing_walk() -> None:
    host = ScriptedHost()
    machine = _machine(host)
    machine.run_delegation("C13", _responder())
    assert host.walked is False
    with pytest.raises(CollectorAbsentError):
        machine.collect_routing(_responder())


def test_a_product_that_declares_no_endpoint_off_the_private_network_leaves_the_control_absent() -> (
    None
):
    host = ScriptedHost(target={})
    machine = _machine(host)
    host.target = {"target": None, "error": "KeyError: backends"}
    with pytest.raises(CollectorAbsentError) as raised:
        machine.run_delegation("C13", _responder())
    assert raised.value.name == "outbound_control"
    assert "KeyError: backends" in raised.value.detail


def test_the_outbound_control_aims_at_the_endpoint_the_installed_contract_declared() -> (
    None
):
    host = ScriptedHost()
    _machine(host).run_delegation("C13", _responder())
    control = [
        a
        for a in host.argvs
        if a[0].endswith("strace") and a[-1] == str(TARGET["port"])
    ]
    assert len(control) == 1
    assert control[0][-2] == TARGET["host"]


# -- the C14 routing walk -------------------------------------------------------------------------------------------------


def _c14(host: ScriptedHost) -> tuple[ModelDelegateObservation, ModelRoutingCollection]:
    machine = _machine(host)
    obs = machine.run_delegation("C14", _responder())
    return obs, machine.collect_routing(_responder())


def _c14_failed(
    obs: ModelDelegateObservation, routing: ModelRoutingCollection
) -> list[str]:
    return [c.name for c in grade_c14(obs, routing) if not c.ok]


def test_a_clean_c14_delegation_and_walk_pass_every_clause() -> None:
    host = ScriptedHost()
    obs, routing = _c14(host)
    assert host.walked is True
    assert _c14_failed(obs, routing) == []
    assert routing.judge_serves_model is True


def test_the_walk_runs_under_the_installed_isolated_interpreter_with_the_customer_environment() -> (
    None
):
    host = ScriptedHost()
    _c14(host)
    walk = [a for a in host.argvs if a[1:3] == ["-I", "-c"] and a[3] == ROUTING_DRIVER]
    assert len(walk) == 1
    assert walk[0][0].endswith("/tool/omnibase-core/bin/python")


def test_an_unbindable_tier_the_walk_found_fails_row2_with_the_finding_named() -> None:
    finding = "a-class/a-tier: backend a-backend is unreachable"
    obs, routing = _c14(ScriptedHost(walk={**WALK_GREEN, "unbindable": [finding]}))
    assert routing.unbindable == (finding,)
    assert _c14_failed(obs, routing) == ["row2_no_unbindable_tier"]


def test_a_deleted_backend_control_that_was_not_classified_deleted_fails_row2() -> None:
    obs, routing = _c14(
        ScriptedHost(walk={**WALK_GREEN, "deleted_control": "bindable"})
    )
    assert routing.undeclared_control_classified_deleted is False
    assert _c14_failed(obs, routing) == ["row2_no_unbindable_tier"]


def test_a_closed_port_control_that_was_not_classified_unreachable_fails_row2() -> None:
    obs, routing = _c14(
        ScriptedHost(walk={**WALK_GREEN, "unreachable_control": "bindable"})
    )
    assert routing.closed_port_control_classified_unreachable is False
    assert _c14_failed(obs, routing) == ["row2_no_unbindable_tier"]


@pytest.mark.parametrize(
    "judge",
    [
        {
            "endpoint_private": False,
            "requires_credential": False,
            "serves_model": False,
        },
        {"endpoint_private": True, "requires_credential": True, "serves_model": True},
        {"endpoint_private": True, "requires_credential": False, "serves_model": False},
        {"error": "RuntimeError: no judge"},
    ],
)
def test_a_reviewer_that_is_metered_or_unserved_or_unresolved_fails_row4(
    judge: dict[str, object],
) -> None:
    obs, routing = _c14(ScriptedHost(walk={**WALK_GREEN, "judge": judge}))
    assert _c14_failed(obs, routing) == ["row4_reviewer_unmetered"]


def test_an_external_connect_in_the_session_fails_row4_and_zero_provider_not_the_walk() -> (
    None
):
    obs, routing = _c14(ScriptedHost(configured_trace=MODEL_CONNECT + EXTERNAL_CONNECT))
    assert _c14_failed(obs, routing) == ["row4_reviewer_unmetered"]


def test_a_local_answer_that_escalated_fails_row1() -> None:
    receipt = _receipt()
    result = cast(
        "dict[str, object]", cast("dict[str, object]", receipt["receipt"])["result"]
    )
    result["escalation_count"] = 1
    obs, routing = _c14(ScriptedHost(receipt=receipt))
    assert "row1_local_answer_ends_chain" in _c14_failed(obs, routing)


def test_a_walk_that_could_not_run_is_an_absent_collector_never_a_fail() -> None:
    walk = {**WALK_GREEN, "collect_error": "ImportError: no module named a_module"}
    machine = _machine(ScriptedHost(walk=walk))
    with pytest.raises(CollectorAbsentError) as raised:
        machine.run_delegation("C14", _responder())
    assert raised.value.name == "routing_walk"
    assert "ImportError" in raised.value.detail


def test_a_walk_that_printed_no_document_is_an_absent_collector() -> None:
    machine = _machine(ScriptedHost(walk_stdout="Traceback (most recent call last)"))
    with pytest.raises(CollectorAbsentError) as raised:
        machine.run_delegation("C14", _responder())
    assert raised.value.name == "routing_walk"


def test_the_drivers_name_no_address_port_or_model() -> None:
    import re

    for source in (TARGET_DRIVER, ROUTING_DRIVER):
        assert re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", source) is None
        assert re.search(r"[\w\]]:\d{2,5}\b", source) is None


def test_a_metrics_endpoint_with_no_token_counter_is_an_absent_instrument() -> None:
    machine = _machine(ScriptedHost(), counter=TokenCounter(exposition=False))
    with pytest.raises(CollectorAbsentError) as raised:
        machine.run_delegation("C13", _responder())
    assert raised.value.name == "model_metrics"


def test_a_metrics_endpoint_that_does_not_answer_is_an_absent_instrument() -> None:
    def refuses(base_url: str) -> str:
        raise OSError("connection refused")

    machine = LocalDelegationCustomerMachine(
        runner=ScriptedHost(),
        metrics_reader=refuses,
        uv="/opt/bin/uv",
        strace="/usr/bin/strace",
        host_env={"PATH": "/usr/bin"},
    )
    with pytest.raises(CollectorAbsentError) as raised:
        machine.run_delegation("C13", _responder())
    assert raised.value.name == "model_metrics"


def test_an_onex_already_on_the_clean_path_refuses_the_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    real = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, path=None: (
            "/usr/bin/onex" if name == "onex" else real(name, path=path)
        ),
    )
    with pytest.raises(CollectorAbsentError) as raised:
        _machine(ScriptedHost()).run_delegation("C13", _responder())
    assert raised.value.name == "clean_machine"


def test_a_host_whose_every_temp_directory_sits_in_a_git_work_tree_is_absent(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    inside = tmp_path / "scratch"
    inside.mkdir()
    machine = LocalDelegationCustomerMachine(
        runner=ScriptedHost(),
        metrics_reader=TokenCounter(),
        uv="/opt/bin/uv",
        strace="/usr/bin/strace",
        host_env={"PATH": "/usr/bin"},
        base_dirs=(inside, tmp_path / "missing"),
    )
    with pytest.raises(CollectorAbsentError) as raised:
        machine.run_delegation("C13", _responder())
    assert raised.value.name == "clean_machine"


# -- the product failing a clause reads FAIL -------------------------------------------------------------------------------


def test_a_failed_install_fails_every_clause_that_needs_it() -> None:
    obs = _machine(ScriptedHost(install_rc=1)).run_delegation("C13", _responder())
    assert obs.rc == 1
    assert {"no_checkout", "install_entry", "three_files", "local_model"} <= set(
        _failed(obs)
    )


def test_an_empty_egress_trace_leaves_the_zero_unproven() -> None:
    host = ScriptedHost(configured_trace="")
    obs = _machine(host).run_delegation("C13", _responder())
    assert obs.egress_recorded is False
    assert _failed(obs) == ["zero_provider"]


def test_a_trace_that_never_saw_the_model_leaves_the_zero_unproven() -> None:
    obs = _machine(ScriptedHost(configured_trace=UNIX_ONLY)).run_delegation(
        "C13", _responder()
    )
    assert obs.model_connects == 0
    assert _failed(obs) == ["zero_provider"]


def test_a_token_counter_that_did_not_move_leaves_the_zero_unproven() -> None:
    obs = _machine(ScriptedHost(), counter=TokenCounter(step=0)).run_delegation(
        "C13", _responder()
    )
    assert obs.token_counter_moved is False
    assert _failed(obs) == ["zero_provider"]


def test_an_external_connect_in_the_session_fails_zero_provider() -> None:
    host = ScriptedHost(configured_trace=MODEL_CONNECT + EXTERNAL_CONNECT)
    obs = _machine(host).run_delegation("C13", _responder())
    assert obs.egress_external == ("external:104.18.2.2:443",)
    assert _failed(obs) == ["zero_provider"]


def test_a_name_lookup_in_the_session_fails_zero_provider() -> None:
    lookup = '4246 connect(7, {sa_family=AF_UNIX, sun_path="/var/run/nscd/socket"}, 110) = -1 ENOENT (No such file or directory)\n'
    obs = _machine(
        ScriptedHost(configured_trace=MODEL_CONNECT + lookup)
    ).run_delegation("C13", _responder())
    assert obs.egress_external == ("name_lookup:/var/run/nscd/socket",)
    assert _failed(obs) == ["zero_provider"]


def test_an_outbound_control_the_parser_cannot_see_fails_zero_provider() -> None:
    obs = _machine(ScriptedHost(control_trace="")).run_delegation("C13", _responder())
    assert obs.control_external_classified is False
    assert _failed(obs) == ["zero_provider"]


def test_an_unconfigured_run_that_answered_is_no_proof_of_the_configuration() -> None:
    obs = _machine(ScriptedHost(unconfigured_rc=0)).run_delegation("C13", _responder())
    assert obs.negative_control_refused is False
    assert _failed(obs) == ["zero_provider"]


def test_an_unconfigured_run_that_reached_the_model_server_fails() -> None:
    obs = _machine(ScriptedHost(unconfigured_trace=MODEL_CONNECT)).run_delegation(
        "C13", _responder()
    )
    assert obs.negative_control_refused is False
    assert _failed(obs) == ["zero_provider"]


def test_a_receipt_naming_another_model_fails_local_model() -> None:
    host = ScriptedHost(receipt=_receipt(model="some-cloud-model"))
    assert _failed(_machine(host).run_delegation("C13", _responder())) == [
        "local_model"
    ]


def test_a_receipt_off_the_local_tier_fails_local_model() -> None:
    host = ScriptedHost(receipt=_receipt(tier="openrouter"))
    assert _failed(_machine(host).run_delegation("C13", _responder())) == [
        "local_model"
    ]


def test_a_distribution_installed_from_a_path_fails_no_checkout() -> None:
    direct = {
        **dict.fromkeys(ONEX_DISTRIBUTIONS),
        "omnimarket": '{"url": "file:///src"}',
    }
    obs = _machine(ScriptedHost(direct_urls=direct)).run_delegation("C13", _responder())
    assert _failed(obs) == ["no_checkout"]


def test_a_distribution_that_is_not_from_the_index_fails_no_checkout() -> None:
    host = ScriptedHost(receipt=_receipt(source="editable"))
    assert _failed(_machine(host).run_delegation("C13", _responder())) == [
        "no_checkout"
    ]


def test_a_source_tree_on_the_machine_fails_no_checkout() -> None:
    obs = _machine(ScriptedHost(plant_checkout=True)).run_delegation(
        "C13", _responder()
    )
    assert _failed(obs) == ["no_checkout"]


def test_a_run_with_no_run_directory_fails_three_files() -> None:
    host = ScriptedHost(receipt={})
    obs = _machine(host).run_delegation("C13", _responder())
    assert "three_files" in _failed(obs)


# -- the green path -------------------------------------------------------------------------------------------------------


def test_a_clean_delegation_through_the_installs_own_entry_passes_every_clause(
    tmp_path: Path,
) -> None:
    runner_bin = tmp_path / "runner-bin"
    runner_bin.mkdir()
    (runner_bin / "onex").write_text("#!/bin/sh\n")
    (runner_bin / "onex").chmod(0o755)
    host = ScriptedHost()
    machine = _machine(host, host_path=f"{runner_bin}:/usr/bin")
    obs = machine.run_delegation("C13", _responder())
    assert _failed(obs) == []
    assert obs.entry_is_install_own is True
    assert obs.entry_path is not None
    assert obs.entry_path != obs.runner_entry_path
    assert obs.runner_entry_path == str(runner_bin / "onex")
    assert obs.negative_control_refused is True
    assert obs.model_connects == 1
    assert machine.versions() == dict.fromkeys(ONEX_DISTRIBUTIONS, "1.0.0")


def test_the_customer_overlay_is_written_after_the_negative_control_and_names_the_responder() -> (
    None
):
    host = ScriptedHost()
    _machine(host).run_delegation("C13", _responder())
    steps = [a for a in host.argvs if a[0].endswith("strace")]
    names = [Path(a[a.index("-o") + 1]).stem for a in steps]
    assert names == ["init", "unconfigured", "configured", "outbound_control"]
    assert host.overlay_text == (
        f"endpoint={ENDPOINT} served={MODEL} window={WINDOW} backends=local-coder,local-heavy-reasoning"
    )


def test_every_step_runs_under_the_connect_tracer_with_a_clean_environment() -> None:
    host = ScriptedHost()
    _machine(host).run_delegation("C13", _responder())
    traced = [a for a in host.argvs if a[0].endswith("strace")]
    assert len(traced) == 4
    for argv in traced:
        assert argv[:6] == ["/usr/bin/strace", "-f", "-qq", "-e", "trace=connect", "-o"]


def test_the_handler_reads_a_clean_c13_and_c14_run_as_pass() -> None:
    reader = InMemoryResponderReader({})
    publisher = InMemoryCheckPublisher()
    gateway_clock = FakeClock()

    reader = InMemoryResponderReader(
        {
            BASE: ModelResponderReading(
                base_url=BASE, answered=True, served_model=MODEL, max_model_len=WINDOW
            )
        }
    )
    handler = HandlerDelegationCheckProbe(
        responders=(ModelDelegationCheckResponder(tier="local", base_url=BASE),),
        reader=reader,
        machine=_machine(ScriptedHost()),
        publisher=publisher,
        topic=TOPIC,
        clock=lambda: datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
        gateway=FakeGateway(),
        gateway_settings=GatewayRunSettings(run_budget_s=5.0, poll_interval_s=0.0),
        sleep=gateway_clock.sleep,
        monotonic=gateway_clock.monotonic,
    )
    c13 = handler.handle(
        ModelDelegationCheckRequest(correlation_id="c13", check_id="C13")
    )
    assert c13.verdict is EnumCheckVerdict.PASS
    assert [c.name for c in c13.clauses if not c.name.startswith("gateway_")] == [
        "no_checkout",
        "install_entry",
        "three_files",
        "local_model",
        "zero_provider",
    ]
    c14 = handler.handle(
        ModelDelegationCheckRequest(correlation_id="c14", check_id="C14")
    )
    assert c14.verdict is EnumCheckVerdict.PASS
    assert [c.name for c in c14.clauses if not c.name.startswith("gateway_")] == [
        "no_checkout",
        "row1_local_answer_ends_chain",
        "row2_no_unbindable_tier",
        "row4_reviewer_unmetered",
    ]
    assert len(publisher.events) == 2


def test_a_clean_machine_with_no_gateway_row_reads_precondition_absent() -> None:
    """Known bad: the machine leg alone never reads PASS; the gateway leg's absent row keeps C13 and C14 unproven."""
    reading = ModelResponderReading(
        base_url=BASE, answered=True, served_model=MODEL, max_model_len=WINDOW
    )
    handler = HandlerDelegationCheckProbe(
        responders=(ModelDelegationCheckResponder(tier="local", base_url=BASE),),
        reader=InMemoryResponderReader({BASE: reading}),
        machine=_machine(ScriptedHost()),
        publisher=InMemoryCheckPublisher(),
        topic=TOPIC,
        clock=lambda: datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
    )
    for check in ("C13", "C14"):
        result = handler.handle(
            ModelDelegationCheckRequest(correlation_id=check.lower(), check_id=check)
        )
        assert result.verdict is EnumCheckVerdict.PRECONDITION_ABSENT
        assert [p.name for p in result.preconditions if not p.present] == [
            "gateway_row"
        ]


# -- the pure readers -----------------------------------------------------------------------------------------------------


def test_parse_connects_classifies_the_model_the_external_and_the_lookup() -> None:
    text = (
        MODEL_CONNECT
        + EXTERNAL_CONNECT
        + (
            '9 connect(4, {sa_family=AF_INET, sin_port=htons(53), sin_addr=inet_addr("127.0.0.53")}, 16) = 0\n'
            "9 connect(5 <unfinished ...>\n"
            "9 <... connect resumed>) = 0\n"
            "9 connect(6, {sa_family=AF_INET, sin_port=htons(80)}, 16) = 0\n"
        )
    )
    kinds = [
        c.kind for c in parse_connects(text, model_host="127.0.0.12", model_port=8000)
    ]
    assert kinds == ["model", "unix_local", "external", "name_lookup", "external"]


def test_a_loopback_model_matches_any_loopback_address_and_a_private_one_only_itself() -> (
    None
):
    line = '1 connect(3, {sa_family=AF_INET, sin_port=htons(8000), sin_addr=inet_addr("127.0.0.1")}, 16) = 0\n'
    assert (
        parse_connects(line, model_host="127.0.0.5", model_port=8000)[0].kind == "model"
    )
    private = line.replace("127.0.0.1", "192.0.2.7")
    assert (
        parse_connects(private, model_host="192.0.2.6", model_port=8000)[0].kind
        == "external"
    )
    assert (
        parse_connects(private, model_host="192.0.2.7", model_port=8000)[0].kind
        == "model"
    )


def test_tokens_from_metrics_reads_llamacpp_then_vllm_and_none_when_absent() -> None:
    assert tokens_from_metrics("llamacpp:tokens_predicted_total 12.0\n") == 12
    assert (
        tokens_from_metrics(
            'vllm:generation_tokens_total{model="m"} 7\nvllm:generation_tokens_total{model="n"} 3\n'
        )
        == 10
    )
    assert tokens_from_metrics("llamacpp:prompt_tokens_total 5\n") is None


def test_find_source_trees_skips_installed_packages(tmp_path: Path) -> None:
    site = tmp_path / "tool" / "lib" / "site-packages" / "omnimarket"
    site.mkdir(parents=True)
    (site / "pyproject.toml").write_text('name = "omnimarket"\n')
    other = tmp_path / "work" / "unrelated"
    other.mkdir(parents=True)
    (other / "pyproject.toml").write_text('name = "left-pad"\n')
    assert find_source_trees([tmp_path]) == ()
    tree = tmp_path / "work" / "omnibase-core"
    tree.mkdir()
    (tree / "pyproject.toml").write_text('[project]\nname = "omnibase-core"\n')
    assert find_source_trees([tmp_path]) == (str(tree),)
