# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The C13 and C14 collector: one clean-container install, the egress recorder and the routing walk, then graded (OMN-26046).

``CleanInstallCustomerMachine.run_delegation`` and ``collect_routing`` raised ``CollectorAbsentError`` for every run, so C13 and
C14 could never grade. These tests drive the collector's pieces over the document the in-machine driver prints (the shape was
read from a real run against a lab responder) and prove the graders on a known good answer (the local model answers and the
chain ends) and on known bad ones (a wrong expected model, an external connect recorded, a blind control).
"""

from __future__ import annotations

import copy
import importlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from _gateway_fake import FakeClock, FakeGateway

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelChainResolution,
    ModelResponderReading,
)
from omnimarket.models.overlay_documents.model_delegation_check_probe_overlay import (
    CheckId,
)
from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckResponder,
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
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.grade_delegation_checks import (
    grade_c13,
    grade_c14,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    HandlerDelegationCheckProbe,
    contract_topics,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.observe_clean_machine_session import (
    delegate_observation_from_document,
    routing_from_document,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    EnumCheckVerdict,
    ModelDelegateObservation,
    ModelDelegationCheckRequest,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    CollectorAbsentError,
)

# The responder row of the settings store, as the chain resolved it. Nothing below names a host of its own: the
# document is built from these values, the way the driver builds its own from the arguments it is handed.
BASE = "http://192.0.2.7:8000"
MODEL = "served-model-x"
WINDOW = 131072
INSTALL = "uv tool install --python 3.12 --with 'omnibase-infra>=0.38.67' --with 'omnimarket>=0.4.205' 'omnibase-core>=0.46.8'"
GUIDE = f'# Quickstart\n\nRequires uv.\n\n```bash\n{INSTALL}\n```\n\nThen run `onex delegate "hello"`.\n'
RESPONDER = ModelChainResolution(
    tier="local", base_url=BASE, served_model=MODEL, window=WINDOW
)
ONEX = "/clean/bin/onex"


def _routing() -> dict[str, Any]:
    tiers = [
        {"tier": "local", "declared": True, "candidates": ["local-coder"]},
        {"tier": "openrouter", "declared": True, "candidates": ["or-1"]},
    ]
    bindable = {"classification": "bindable"}
    return {
        "public_task_classes": ["document", "review"],
        "public_task_classes_from_contract_yaml": ["review", "document"],
        "walk": {"document": tiers, "review": tiers},
        "backends": {"local-coder": bindable, "or-1": bindable},
        "controls": [
            {"name": "deleted_backend_control", "classification": "deleted_backend"},
            {"name": "unreachable_endpoint_control", "classification": "unreachable"},
        ],
        "judge": {
            "backend_id": "local-coder",
            "endpoint_private": True,
            "requires_credential": False,
            "classification": "bindable",
        },
    }


def _document() -> dict[str, Any]:
    host = BASE.split("//")[1]
    attempt = {
        "tier": "local",
        "acceptance_decision": "accept",
        "attempt_kind": "first_try",
        "model_id": MODEL,
        "host": host.split(":")[0],
    }
    return {
        "python": "3.12.8",
        "isolated_interpreter": True,
        "versions": {"omnibase-core": "0.47.79", "omnimarket": "0.4.327"},
        "distributions": {
            "omnibase-core": {"installed": True, "version": "1.0", "direct_url": False},
            "omnibase-infra": {
                "installed": True,
                "version": "1.0",
                "direct_url": False,
            },
            "omnimarket": {
                "installed": True,
                "version": "1.0",
                "direct_url": False,
                "path": "/clean/site-packages/omnimarket",
                "in_site_packages": True,
                "git_ancestor": None,
            },
            **{
                module: {
                    "path": "/clean/site-packages/x",
                    "in_site_packages": True,
                    "git_ancestor": None,
                }
                for module in ("omnibase_infra", "omnibase_core")
            },
        },
        "entry": {"resolved": ONEX, "install_own": ONEX},
        "run": {
            "exit_code": 0,
            "three_files_agree": True,
            "egress": [
                {"method": "GET", "target": f"{BASE}/v1/models", "private": True},
                {
                    "method": "POST",
                    "target": f"{BASE}/v1/chat/completions",
                    "private": True,
                },
            ],
            "control_egress": [
                {"method": "GET", "target": "http://external.test/", "private": False}
            ],
            "receipt": {"endpoint": f"{BASE}/v1/chat/completions", "model": MODEL},
            "receipt_result": {
                "attempts": [attempt],
                "escalation_count": 0,
                "model_name": MODEL,
                "metrics": {"cost_usd": 0.0, "input_tokens": 312, "output_tokens": 118},
            },
        },
        "routing": _routing(),
    }


def _grades(
    document: Mapping[str, Any], *, expected_model: str = MODEL
) -> dict[str, dict[str, bool]]:
    obs = delegate_observation_from_document(document, responder_base=BASE)
    routing = routing_from_document(document)
    return {
        "C13": {
            c.name: c.ok
            for c in grade_c13(obs, responder_base=BASE, served_model=expected_model)
        },
        "C14": {c.name: c.ok for c in grade_c14(obs, routing)},
    }


# ----- known good ------------------------------------------------------------------------------------------------------


def test_a_local_answer_that_ends_the_chain_passes_c13_and_c14() -> None:
    grades = _grades(_document())
    assert all(grades["C13"].values()), grades["C13"]
    assert all(grades["C14"].values()), grades["C14"]


# ----- known bad -------------------------------------------------------------------------------------------------------


def test_a_wrong_expected_model_fails_the_local_model_clause() -> None:
    grades = _grades(_document(), expected_model="some-other-model")
    assert grades["C13"]["local_model"] is False
    assert grades["C13"]["three_files"] is True


def test_an_external_connect_recorded_fails_zero_provider_and_the_reviewer_row() -> (
    None
):
    document = _document()
    document["run"]["egress"].append(
        {"method": "CONNECT", "target": "api.vendor.test:443", "private": False}
    )
    grades = _grades(document)
    assert grades["C13"]["zero_provider"] is False
    assert grades["C14"]["row4_reviewer_unmetered"] is False
    assert grades["C14"]["row1_local_answer_ends_chain"] is True


def test_a_control_connect_that_was_not_classified_external_fails_zero_provider() -> (
    None
):
    document = _document()
    document["run"]["control_egress"] = [
        {"method": "GET", "target": "http://external.test/", "private": True}
    ]
    assert _grades(document)["C13"]["zero_provider"] is False


def test_a_recorder_that_saw_nothing_fails_zero_provider() -> None:
    document = _document()
    document["run"]["egress"] = []
    assert _grades(document)["C13"]["zero_provider"] is False


def test_a_recorder_blind_to_the_responder_fails_zero_provider() -> None:
    document = _document()
    document["run"]["egress"] = [
        {"method": "GET", "target": "http://other.test:1/", "private": True}
    ]
    assert _grades(document)["C13"]["zero_provider"] is False


def test_a_distribution_installed_from_a_path_fails_no_checkout() -> None:
    document = _document()
    document["distributions"]["omnimarket"]["direct_url"] = True
    grades = _grades(document)
    assert grades["C13"]["no_checkout"] is False
    assert grades["C14"]["no_checkout"] is False


def test_a_module_under_a_git_tree_fails_no_checkout() -> None:
    document = _document()
    document["distributions"]["omnibase_core"]["git_ancestor"] = "/somewhere"
    assert _grades(document)["C13"]["no_checkout"] is False


def test_an_onex_that_is_not_the_installs_own_fails_install_entry() -> None:
    document = _document()
    document["entry"]["resolved"] = "/usr/bin/onex"
    assert _grades(document)["C13"]["install_entry"] is False


def test_an_escalated_run_fails_row_one() -> None:
    document = _document()
    document["run"]["receipt_result"]["escalation_count"] = 1
    document["run"]["receipt_result"]["attempts"].append(
        {
            "tier": "openrouter",
            "acceptance_decision": "accept",
            "model_id": "m",
            "host": "x",
        }
    )
    grades = _grades(document)
    assert grades["C14"]["row1_local_answer_ends_chain"] is False
    assert grades["C13"]["local_model"] is False


def test_a_tier_whose_backend_is_not_bindable_fails_row_two() -> None:
    document = _document()
    document["routing"]["backends"]["or-1"] = {"classification": "model_not_served"}
    grades = _grades(document)
    assert grades["C14"]["row2_no_unbindable_tier"] is False


def test_a_class_with_no_local_first_rung_fails_row_two() -> None:
    document = _document()
    document["routing"]["walk"]["review"] = list(
        reversed(document["routing"]["walk"]["review"])
    )
    assert _grades(document)["C14"]["row2_no_unbindable_tier"] is False


def test_a_blind_classifier_control_fails_row_two() -> None:
    document = _document()
    document["routing"]["controls"][0]["classification"] = "bindable"
    assert _grades(document)["C14"]["row2_no_unbindable_tier"] is False


def test_a_reviewer_that_needs_a_credential_fails_row_four() -> None:
    document = _document()
    document["routing"]["judge"]["requires_credential"] = True
    assert _grades(document)["C14"]["row4_reviewer_unmetered"] is False


def test_an_unresolvable_reviewer_fails_row_four_not_absent() -> None:
    document = _document()
    document["routing"]["judge"] = {"error": "ImportError: gone"}
    assert _grades(document)["C14"]["row4_reviewer_unmetered"] is False


def test_a_routing_walk_that_could_not_run_is_an_absent_instrument() -> None:
    document = _document()
    document["routing"] = {"collect_error": "ImportError: no module"}
    with pytest.raises(CollectorAbsentError):
        routing_from_document(document)


# ----- the machine, end to end over the in-machine document -----------------------------------------------------------


class _Sources:
    def guide(self) -> tuple[str, str]:
        return "guides/onex-plugin-quickstart.md", GUIDE

    def registry(self) -> Mapping[str, Any]:
        return {}


class _Runner:
    def __init__(self, document: dict[str, Any], *, install_rc: int = 0) -> None:
        self._document = document
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
        return CommandResult(0, json.dumps(self._document), "")


def _uv(tmp_path: Path) -> str:
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n")
    uv.chmod(0o755)
    return str(uv)


def _machine(tmp_path: Path, runner: _Runner) -> CleanInstallCustomerMachine:
    return CleanInstallCustomerMachine(
        sources=_Sources(),
        runner=runner,
        uv=_uv(tmp_path),
        host_env={"HTTPS_PROXY": "http://proxy.invalid:3128"},
    )


_CLOCK = FakeClock()


def _handler(machine: CleanInstallCustomerMachine) -> HandlerDelegationCheckProbe:
    return HandlerDelegationCheckProbe(
        responders=(ModelDelegationCheckResponder(tier="local", base_url=BASE),),
        reader=InMemoryResponderReader(
            {
                BASE: ModelResponderReading(
                    base_url=BASE,
                    answered=True,
                    served_model=MODEL,
                    max_model_len=WINDOW,
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


def _request(check: CheckId) -> ModelDelegationCheckRequest:
    return ModelDelegationCheckRequest(correlation_id="x", check_id=check)


def _in_machine_document(runner: _Runner) -> dict[str, Any]:
    return runner._document


@pytest.mark.parametrize("check", ["C13", "C14"])
def test_a_clean_install_run_reads_pass_for_a_good_document(
    tmp_path: Path, check: CheckId
) -> None:
    runner = _Runner(_document())
    result = _handler(_machine(tmp_path, runner)).handle(_request(check))
    assert result.verdict is EnumCheckVerdict.PASS, [
        (c.name, c.detail) for c in result.clauses if not c.ok
    ]


def test_the_driver_is_handed_the_responder_row_and_runs_on_the_installs_own_path(
    tmp_path: Path,
) -> None:
    runner = _Runner(_document())
    _handler(_machine(tmp_path, runner)).handle(_request("C13"))
    driver_call = next(c for c in runner.calls if c[0][0] != "uv")
    argv = driver_call[0]
    assert argv[argv.index("--base-url") + 1] == BASE
    assert argv[argv.index("--served-model") + 1] == MODEL
    assert argv[argv.index("--window") + 1] == str(WINDOW)
    env = driver_call[1]
    assert env["PATH"].split(":")[0].endswith("/bin")
    assert "-I" in argv
    assert driver_call[2] is not None
    assert "EgressRecorder" in driver_call[2]


def test_c13_and_c14_in_one_tick_install_once(tmp_path: Path) -> None:
    runner = _Runner(_document())
    handler = _handler(_machine(tmp_path, runner))
    handler.handle(_request("C13"))
    handler.handle(_request("C14"))
    assert sum(1 for c in runner.calls if c[0][0] == "uv") == 1


def test_a_known_bad_external_connect_reads_fail_through_the_machine(
    tmp_path: Path,
) -> None:
    document = _document()
    document["run"]["egress"].append(
        {"method": "CONNECT", "target": "api.vendor.test:443", "private": False}
    )
    result = _handler(_machine(tmp_path, _Runner(document))).handle(_request("C13"))
    assert result.verdict is EnumCheckVerdict.FAIL
    assert [c.name for c in result.clauses if not c.ok] == ["zero_provider"]


def test_an_install_that_exits_nonzero_reads_fail_not_absent(tmp_path: Path) -> None:
    result = _handler(_machine(tmp_path, _Runner(_document(), install_rc=1))).handle(
        _request("C13")
    )
    assert result.verdict is EnumCheckVerdict.FAIL


def test_a_driver_that_prints_nothing_reads_precondition_absent(tmp_path: Path) -> None:
    class _Silent(_Runner):
        def run(
            self,
            argv: Sequence[str],
            *,
            env: Mapping[str, str],
            cwd: Path,
            timeout_s: float,
            stdin: str | None = None,
        ) -> CommandResult:
            if argv[0] == "uv":
                return CommandResult(0, "", "")
            return CommandResult(1, "", "Traceback")

    result = _handler(_machine(tmp_path, _Silent({}))).handle(_request("C14"))
    assert result.verdict is EnumCheckVerdict.PRECONDITION_ABSENT


def test_the_observation_never_aliases_the_document() -> None:
    document = _document()
    before = copy.deepcopy(document)
    delegate_observation_from_document(document, responder_base=BASE)
    routing_from_document(document)
    assert document == before
    assert isinstance(
        delegate_observation_from_document(document, responder_base=BASE),
        ModelDelegateObservation,
    )


DRIVER_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_delegation_check_probe_effect/handlers"
    / "clean_machine_routing_driver.py"
)
SUPPRESSION = re.compile(
    r"#\s*(noqa\b|type:\s*ignore\b|pragma:\s*(no\s+cover|allowlist)\b|nosec\b"
    r"|[A-Za-z][\w-]*-ok\b)"
)


def test_the_driver_reads_no_process_environment() -> None:
    """The delegation env-read gate refuses os.environ in a delegation node; the driver is handed its environment."""
    sys.path.insert(0, str(DRIVER_PATH.parents[5] / "scripts" / "ci"))
    scanner = importlib.import_module("check_delegation_env_reads")
    source = DRIVER_PATH.read_text(encoding="utf-8")
    found = scanner._find_env_calls_in_source(source, str(DRIVER_PATH))
    assert found == []


def test_the_driver_carries_no_suppression_comment() -> None:
    """The canonical-file-shape ratchet counts suppression comments against base 0; fix the code instead."""
    hits = [
        line
        for line in DRIVER_PATH.read_text(encoding="utf-8").splitlines()
        if SUPPRESSION.search(line)
    ]
    assert hits == []


def test_the_session_environment_reaches_the_driver_by_file(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    class _Reading(_Runner):
        def run(
            self,
            argv: Sequence[str],
            *,
            env: Mapping[str, str],
            cwd: Path,
            timeout_s: float,
            stdin: str | None = None,
        ) -> CommandResult:
            if "--env-file" in argv:
                seen["env"] = json.loads(
                    Path(argv[argv.index("--env-file") + 1]).read_text()
                )
            return super().run(argv, env=env, cwd=cwd, timeout_s=timeout_s, stdin=stdin)

    _handler(_machine(tmp_path, _Reading(_document()))).handle(_request("C13"))
    assert seen["env"]["PATH"].split(":")[0].endswith("/bin")
