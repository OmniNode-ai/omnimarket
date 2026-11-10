# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The published walk receipt carries no credential, and a lost clone reply still cleans up.

Every planted value is fake and assembled at runtime so no scanner reads a
credential-shaped literal in this file.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest

from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers import (
    handler_onboarding_clean_vm_walk as walk,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_clean_vm_walk import (
    EnumWalkOutcome,
    EnumWalkStepKind,
    ModelRunnerResult,
    ModelWalkReceipt,
    ModelWalkRequest,
)

pytestmark = pytest.mark.unit

LLM_HOST = "llm.example.invalid"
GIT_HOST = "git.example.invalid"
VM_NAME = "onex-walk-feedc0de"
GUEST_IP = "192.0.2.77"
GUEST_ASKPASS = "/opt/walk/askpass"


def fake(*parts: str) -> str:
    return "".join(parts)


USER = fake("walk", "er")
URL_PASSWORDS = {
    "local_url": fake("lu-", "pass", "-7Qx"),
    "branch": fake("br-", "pass", "-3Kd"),
    "log": fake("lg-", "pass", "-9Wm"),
    "failure": fake("fl-", "pass", "-2Zt"),
}
URL_TOKEN = fake("tok", "en", "inurl", "5Rv8")
ASSIGNED = fake("as-", "sign", "ed-4Np")
HEADER = fake("hd", "rvalue", "8Lq2", "Yt")
PROVIDER_KEYS = {
    "openai": fake("sk", "-", "proj", "-", "Z" * 24),
    "github": fake("gh", "p_", "Q" * 36),
    "aws": fake("AK", "IA", "FAKEFIXTURE00000"),
    "slack": fake("xo", "xb-", "1" * 12),
}
PEM_BODY = fake("MIIfake", "body", "Line", "Xy9")
PEM = fake(
    "-----BEGIN ",
    "OPENSSH ",
    "PRIVATE ",
    "KEY-----\n",
    PEM_BODY,
    "\n",
    "-----END ",
    "OPENSSH ",
    "PRIVATE ",
    "KEY-----",
)

PHASES_LOG = (
    "21:04:03 ▶ Phase 0/6: Preflight\n"
    "  macOS                  26.6.2           14+            14+\n"
    "21:04:04 ✔ Phase 0/6 PASSED: Preflight (1s)\n"
    '{"skill_name":"node_delegate_skill_orchestrator","status":"success",'
    '"run_id":"success-1","result":{"terminal_payload":{"backend":"local"}}}\n'
    "21:09:32 ✔ Phase 6/6 PASSED: Complete (1s)\n"
)


def request(**overrides: object) -> ModelWalkRequest:
    return ModelWalkRequest.model_validate(
        {
            "script_sha": "a" * 40,
            "checksum_line": "b" * 64 + "  lab-onboarding.sh",
            "providers": ["local"],
            "local_url": "http://127.0.0.1:8000/v1",
            "local_model": "test model",
            "tart_host": "192.0.2.1",
            "vm_name": VM_NAME,
            "guest_askpass_path": GUEST_ASKPASS,
            "guest_ip": GUEST_IP,
            **overrides,
        }
    )


class ScriptedRunner:
    """Tart transport scripted per step kind; nothing leaves the test process."""

    def __init__(self, req: ModelWalkRequest, log: str = PHASES_LOG) -> None:
        self.request = walk.resolve_request(req)
        self.log = log
        self.calls: list[EnumWalkStepKind] = []
        self.scripted: dict[EnumWalkStepKind, list[ModelRunnerResult | Exception]] = {}

    def _kind(self, argv: Sequence[str]) -> EnumWalkStepKind:
        return next(
            kind
            for kind in EnumWalkStepKind
            if tuple(argv) == walk.build_step_argv(kind, self.request)
        )

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ModelRunnerResult:
        if argv[-1] == "--version":
            return ModelRunnerResult(exit_code=0, stdout="tart 2.0", stderr="")
        kind = self._kind(argv)
        self.calls.append(kind)
        if self.scripted.get(kind):
            outcome = self.scripted[kind].pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        stdout = ""
        if kind == EnumWalkStepKind.TART_IP:
            stdout = f"{GUEST_IP}\n"
        if kind in (EnumWalkStepKind.GUEST_RUN_SCRIPT, EnumWalkStepKind.GUEST_READ_LOG):
            stdout = self.log
        if kind in (
            EnumWalkStepKind.GUEST_DASHBOARD_PROBE_ROOT,
            EnumWalkStepKind.GUEST_DASHBOARD_PROBE_PROJECTIONS,
        ):
            stdout = "200 http://127.0.0.1:8123\n"
        return ModelRunnerResult(exit_code=0, stdout=stdout, stderr="")

    def start(self, argv: Sequence[str]) -> None:
        self.calls.append(self._kind(argv))


def walk_receipt(req: ModelWalkRequest, runner: ScriptedRunner) -> ModelWalkReceipt:
    return walk.HandlerOnboardingCleanVmWalk(runner).handle(req)


def assert_absent(dumped: str, *values: str) -> None:
    leaked = [len(v) for v in values if v in dumped]
    assert not leaked, f"{len(leaked)} planted value(s) survived, lengths {leaked}"


def test_redact_url_userinfo_from_every_field() -> None:
    local_url = f"http://{USER}:{URL_PASSWORDS['local_url']}@{LLM_HOST}:8000/v1"
    branch = (
        f"omniclaude=https://{USER}:{URL_PASSWORDS['branch']}@{GIT_HOST}/o/r.git#main"
    )
    log = (
        PHASES_LOG
        + f"cloning https://{USER}:{URL_PASSWORDS['log']}@{GIT_HOST}/o/plugins.git\n"
        + f"fetching https://{URL_TOKEN}@{GIT_HOST}/o/skills.git\n"
    )
    req = request(local_url=local_url, branches_installed=[branch])
    runner = ScriptedRunner(req, log)
    receipt = walk_receipt(req, runner)
    assert receipt.outcome == EnumWalkOutcome.PASSED, receipt.failure_text
    run_step = next(
        s for s in receipt.steps if s.kind == EnumWalkStepKind.GUEST_RUN_SCRIPT
    )
    assert LLM_HOST in run_step.argv[-1]
    dumped = receipt.model_dump_json()
    assert_absent(dumped, USER + ":", *URL_PASSWORDS.values(), URL_TOKEN)
    assert receipt.local_url == f"http://{LLM_HOST}:8000/v1"
    assert receipt.branches_installed == (
        f"omniclaude=https://{GIT_HOST}/o/r.git#main",
    )
    assert f"https://{GIT_HOST}/o/skills.git" in receipt.log_tail


def test_redact_secret_shapes_from_log_tail() -> None:
    log = (
        PHASES_LOG
        + f"export OPENAI_API_KEY={ASSIGNED}\n"
        + f'config: {{"password": "{ASSIGNED}x"}}\n'
        + f"  secret: '{ASSIGNED}y'\n"
        + f"curl -H 'Authorization: Bearer {HEADER}' https://{LLM_HOST}/v1/models\n"
        + "keys: "
        + " ".join(PROVIDER_KEYS.values())
        + "\n"
        + PEM
        + "\ndone\n"
    )
    req = request()
    receipt = walk_receipt(req, ScriptedRunner(req, log))
    assert receipt.outcome == EnumWalkOutcome.PASSED, receipt.failure_text
    dumped = receipt.model_dump_json()
    assert_absent(dumped, ASSIGNED, HEADER, PEM_BODY, *PROVIDER_KEYS.values())
    assert "export OPENAI_API_KEY=" in receipt.log_tail
    assert f"https://{LLM_HOST}/v1/models" in receipt.log_tail
    assert receipt.log_tail.endswith("done\n")


def test_redact_failure_text_and_terminal_cause() -> None:
    secret_url = f"https://{USER}:{URL_PASSWORDS['failure']}@{GIT_HOST}/o/r.git"
    req = request()
    runner = ScriptedRunner(req)
    runner.scripted[EnumWalkStepKind.GUEST_RUN_SCRIPT] = [
        ModelRunnerResult(
            exit_code=1,
            stdout="",
            stderr=f"fatal: could not read {secret_url} (token {PROVIDER_KEYS['github']})",
        )
    ]
    receipt = walk_receipt(req, runner)
    assert receipt.outcome == EnumWalkOutcome.FAILED
    assert GIT_HOST in (receipt.failure_text or "")
    assert receipt.terminal_failure_cause == receipt.failure_text
    assert_absent(
        receipt.model_dump_json(), URL_PASSWORDS["failure"], PROVIDER_KEYS["github"]
    )


def test_redact_a_key_block_cut_by_the_tail_boundary() -> None:
    # The key block starts before the last 4000 characters and ends inside them.
    log = PHASES_LOG + PEM[:40] + "\n" + (PEM_BODY + "\n") * 400 + PEM[40:] + "\n"
    req = request()
    receipt = walk_receipt(req, ScriptedRunner(req, log))
    assert_absent(receipt.model_dump_json(), PEM_BODY)


def test_lost_clone_timeout_still_deletes_the_vm() -> None:
    req = request()
    runner = ScriptedRunner(req)
    runner.scripted[EnumWalkStepKind.TART_CLONE] = [
        subprocess.TimeoutExpired(cmd="tart clone", timeout=3600)
    ]
    receipt = walk_receipt(req, runner)
    assert receipt.outcome == EnumWalkOutcome.FAILED
    assert runner.calls == [
        EnumWalkStepKind.TART_CLONE,
        EnumWalkStepKind.TART_STOP,
        EnumWalkStepKind.TART_DELETE,
    ]
    assert receipt.vm_deleted is True


def test_lost_clone_ssh_timeout_reply_still_deletes_the_vm() -> None:
    # The SSH transport turns a timeout into exit 124 rather than raising.
    req = request()
    runner = ScriptedRunner(req)
    runner.scripted[EnumWalkStepKind.TART_CLONE] = [
        ModelRunnerResult(exit_code=124, stdout="", stderr="SSH command timed out")
    ]
    receipt = walk_receipt(req, runner)
    assert EnumWalkStepKind.TART_DELETE in runner.calls
    assert receipt.vm_deleted is True


def test_lost_clone_of_a_never_created_vm_cleans_up_quietly() -> None:
    req = request()
    runner = ScriptedRunner(req)
    absent = ModelRunnerResult(
        exit_code=1,
        stdout="",
        stderr=f'Error: the specified VM "{VM_NAME}" does not exist',
    )
    runner.scripted[EnumWalkStepKind.TART_CLONE] = [
        subprocess.TimeoutExpired(cmd="tart clone", timeout=3600)
    ]
    runner.scripted[EnumWalkStepKind.TART_STOP] = [absent]
    runner.scripted[EnumWalkStepKind.TART_DELETE] = [absent]
    receipt = walk_receipt(req, runner)
    assert EnumWalkStepKind.TART_DELETE in runner.calls
    assert receipt.vm_deleted is True
    assert "does not exist" not in (receipt.failure_text or "")


def test_lost_clone_with_refused_delete_names_the_vm_undeleted() -> None:
    req = request()
    runner = ScriptedRunner(req)
    runner.scripted[EnumWalkStepKind.TART_CLONE] = [
        subprocess.TimeoutExpired(cmd="tart clone", timeout=3600)
    ]
    runner.scripted[EnumWalkStepKind.TART_DELETE] = [
        ModelRunnerResult(exit_code=1, stdout="", stderr="delete refused: VM is locked")
    ]
    receipt = walk_receipt(req, runner)
    assert EnumWalkStepKind.TART_DELETE in runner.calls
    assert receipt.vm_deleted is False
    assert f"{VM_NAME} left undeleted" in (receipt.failure_text or "")


def test_refused_clone_of_an_existing_vm_name_leaves_that_vm_alone() -> None:
    # The caller-supplied name belongs to a VM this walk did not create.
    req = request()
    runner = ScriptedRunner(req)
    runner.scripted[EnumWalkStepKind.TART_CLONE] = [
        ModelRunnerResult(
            exit_code=1,
            stdout="",
            stderr=f'Error: VM "{VM_NAME}" already exists',
        )
    ]
    receipt = walk_receipt(req, runner)
    assert receipt.outcome == EnumWalkOutcome.FAILED
    assert runner.calls == [EnumWalkStepKind.TART_CLONE]
    assert receipt.vm_deleted is False
    failure = receipt.failure_text or ""
    assert f"VM {VM_NAME} was not created by this walk" in failure
    assert "left undeleted" not in failure


def test_clone_refused_before_it_started_leaves_the_vm_alone() -> None:
    # The step guard refuses the clone before any command reaches Tart.
    req = request()
    runner = ScriptedRunner(req)
    runner.scripted[EnumWalkStepKind.TART_CLONE] = [
        walk.StepNotDocumentedError("tart_clone: step not documented")
    ]
    receipt = walk_receipt(req, runner)
    assert EnumWalkStepKind.TART_STOP not in runner.calls
    assert EnumWalkStepKind.TART_DELETE not in runner.calls
    assert receipt.vm_deleted is False


def test_lost_clone_ssh_failure_exit_still_deletes_the_vm() -> None:
    # SSH exits 255 when the connection drops; the clone may have completed.
    req = request()
    runner = ScriptedRunner(req)
    runner.scripted[EnumWalkStepKind.TART_CLONE] = [
        ModelRunnerResult(exit_code=255, stdout="", stderr="Connection reset")
    ]
    receipt = walk_receipt(req, runner)
    assert EnumWalkStepKind.TART_DELETE in runner.calls
    assert receipt.vm_deleted is True
