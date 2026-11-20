# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A runtime push whose contract no hosted deploy-gate probe satisfies is refused at the push (OMN-25723).

The hosted ``deploy-gate / deploy-gate`` check passes a runtime change only when a
``check_value`` of the ticket's contract is a live-surface probe in command position.
It resolves the head of a command through ``env``, ``sudo``, ``timeout`` and the like
but not through ``uv run``, so ``uv run onex lab-work run ...`` heads at ``uv`` and is
refused there. The integration-proof check resolved it through ``uv run`` and passed the
push, so omnimarket pull requests 6317, 6374 and 6403 reached the required check red.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers import (
    handler_ticket_contract_prerequisite as boundary,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers.deploy_gate_probe import (
    is_hosted_falsifiable,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers.deploy_gate_probe_check import (
    check_deploy_gate_probe,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.models.model_integration_proof_request import (
    ModelIntegrationProofRequest,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.models.model_integration_proof_result import (
    ModelIntegrationProofResult,
)

pytestmark = pytest.mark.unit
TICKET = "OMN-25723"
RUNTIME = ("src/omnimarket/nodes/node_x/handlers/handler_x.py",)
LAB_RUN = "lab-work run --repo OmniNode-ai/omnimarket --sha HEAD --lane l -- pytest tests/test_x.py"
GH_API_PROBE = (
    "bash -o pipefail -c 'gh api \"repos/OmniNode-ai/omnimarket/contents/"
    'src/omnimarket/nodes/node_x/handlers/handler_x.py?ref=$(git rev-parse HEAD)" '
    '--jq .content | base64 -d | grep -F "HandlerX" > /dev/null\''
)


@pytest.fixture(autouse=True)
def _no_inherited_git_location(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "command",
    [
        f"onex {LAB_RUN}",
        f"env PYTHONDONTWRITEBYTECODE=1 onex {LAB_RUN}",
        f"timeout 600 onex {LAB_RUN}",
        "docker exec omninode-runtime python -c 'import omnimarket'",
        "rpk topic consume t -n 1 | grep -q payload",
        GH_API_PROBE,
    ],
)
def test_a_probe_the_hosted_gate_accepts_is_accepted(command: str) -> None:
    assert is_hosted_falsifiable(command)


@pytest.mark.parametrize(
    "command",
    [
        f"uv run onex {LAB_RUN}",
        "uv run onex run-node node_x --input in.json",
        "uv run --frozen onex lab-work run --lane l -- pytest tests/test_x.py",
        "echo onex lab-work run",
        "git show HEAD:src/x.py | grep -F HandlerX",
        'on_host() { ssh "$1" true; }; on_host h201',
        "grep -q '^status: PASS$' drift/dod_receipts/OMN-1/dod-deploy/command.yaml",
        "onex ledger query --file contracts/OMN-25723.yaml",
        "",
    ],
)
def test_a_probe_the_hosted_gate_refuses_is_refused(command: str) -> None:
    assert not is_hosted_falsifiable(command)


def _contract(*commands: str) -> str:
    checks = "".join(
        f"      - check_type: command\n        check_value: {json.dumps(c)}\n"
        for c in commands
    )
    return (
        f"ticket_id: {TICKET}\n"
        "dod_evidence:\n"
        "  - id: dod-omn25723-integration-proof\n"
        "    description: the change ran on a lab host\n"
        "    source: manual\n"
        "    execution_scope: local_done_gate\n"
        "    binds_ac: [AC1]\n"
        "    checks:\n" + checks
    )


def _run(
    contract: str, paths: tuple[str, ...] = RUNTIME
) -> ModelIntegrationProofResult:
    return check_deploy_gate_probe(
        ModelIntegrationProofRequest(
            ticket_id=TICKET, contract_text=contract, changed_paths=paths
        )
    )


def test_a_contract_whose_only_probe_heads_at_uv_is_refused() -> None:
    result = _run(_contract(f"uv run onex {LAB_RUN}"))
    assert not result.passed
    assert result.failure is not None
    assert "deploy-gate / deploy-gate fails" in result.failure
    assert "`uv run`" in result.failure
    assert RUNTIME[0] in result.failure


def test_a_contract_with_one_accepted_probe_among_refused_ones_passes() -> None:
    result = _run(_contract(f"uv run onex {LAB_RUN}", f"onex {LAB_RUN}"))
    assert result.passed
    assert result.runtime_ticket


def test_a_contract_with_the_gh_api_deploy_probe_passes() -> None:
    assert _run(_contract(GH_API_PROBE)).passed


def test_a_change_that_runs_nothing_owes_no_hosted_probe() -> None:
    result = _run(_contract(f"uv run onex {LAB_RUN}"), ("docs/plan.md", "tests/t.py"))
    assert result.passed
    assert not result.runtime_ticket


def test_an_unreadable_contract_is_refused_naming_the_file() -> None:
    result = _run("ticket_id: OMN-25723\ndod_evidence: [unclosed\n")
    assert result.failure is not None
    assert f"contracts/{TICKET}.yaml" in result.failure


def _git_repo(tmp_path: Path, contract: str, changed: str) -> tuple[str, str]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
            env=scrub_git_location_env(os.environ),
        ).stdout.strip()

    git("init", "-q", "-b", "base")
    (tmp_path / "README.md").write_text("x\n")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-q", "-b", "OMN-25723-work")
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / f"{TICKET}.yaml").write_text(contract)
    target = tmp_path / changed
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", f"feat({TICKET}): change")
    return git("rev-parse", "HEAD"), base


def _contract_with_ac(contract: str) -> str:
    return (
        "requirements:\n"
        "  - id: req\n"
        "    statement: s\n"
        "    acceptance:\n"
        "      - id: AC1\n"
        "        statement: 'AC1: x -- falsifier: uv run pytest tests/test_x.py -q'\n"
        + contract
    )


def test_the_push_check_refuses_a_uv_run_only_probe_through_real_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _contract_with_ac(_contract(f"uv run onex {LAB_RUN}"))
    head, base = _git_repo(tmp_path, contract, RUNTIME[0])
    monkeypatch.chdir(tmp_path)
    failures = boundary.check(head, base, f"feat({TICKET}): change")
    assert len(failures) == 1
    assert "deploy-gate / deploy-gate fails" in failures[0]


def test_the_push_check_passes_an_accepted_probe_through_real_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _contract_with_ac(_contract(f"onex {LAB_RUN}"))
    head, base = _git_repo(tmp_path, contract, RUNTIME[0])
    monkeypatch.chdir(tmp_path)
    assert boundary.check(head, base, f"feat({TICKET}): change") == ()
