# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A runtime ticket's contract carries an integration proof or is refused (OMN-21887)."""

from __future__ import annotations

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
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers.handler_integration_proof import (
    HandlerIntegrationProof,
    runtime_paths,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.models.model_integration_proof_request import (
    ModelIntegrationProofRequest,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.models.model_integration_proof_result import (
    ModelIntegrationProofResult,
)

pytestmark = pytest.mark.unit
TICKET = "OMN-21887"
RUNTIME = ("src/omnimarket/nodes/node_x/handlers/handler_x.py",)

UNIT_ITEM = """
  - id: dod-omn21887-unit
    description: unit tests
    source: manual
    binds_ac: [AC1]
    checks:
      - check_type: test_passes
        check_value: uv run pytest tests/test_x.py -q
"""


def _proof_item(
    *,
    item_id: str = "dod-omn21887-integration-proof",
    scope: str = "local_done_gate",
    check_type: str = "command",
    command: str = "uv run onex lab-work --lane l --repo omnimarket --ref b -- uv run pytest tests/test_x.py",
) -> str:
    return f"""
  - id: {item_id}
    description: the change ran on a lab host with real dependencies
    source: manual
    execution_scope: {scope}
    binds_ac: [AC1]
    checks:
      - check_type: {check_type}
        check_value: "{command}"
"""


def _contract(*items: str) -> str:
    return (
        f"ticket_id: {TICKET}\ndod_evidence:" + "".join(items)
        if items
        else f"ticket_id: {TICKET}\ndod_evidence: []\n"
    )


def _run(
    contract: str, paths: tuple[str, ...] = RUNTIME
) -> ModelIntegrationProofResult:
    return HandlerIntegrationProof().handle(
        ModelIntegrationProofRequest(
            ticket_id=TICKET, contract_text=contract, changed_paths=paths
        )
    )


@pytest.mark.parametrize(
    ("path", "runs"),
    [
        ("src/omnimarket/nodes/node_x/handlers/handler_x.py", True),
        ("src/omnimarket/nodes/node_x/contract.yaml", True),
        ("Dockerfile.runtime", True),
        ("docker/compose.yml", True),
        ("src/omnimarket/nodes/node_x/tests/test_x.py", False),
        ("tests/test_x.py", False),
        ("docs/README.md", False),
        ("src/omnimarket/nodes/node_x/README.md", False),
        ("contracts/OMN-21887.yaml", False),
        (".github/workflows/ci.yml", False),
        ("scripts/ci/check.py", False),
    ],
)
def test_only_paths_that_run_count_as_runtime(path: str, runs: bool) -> None:
    assert bool(runtime_paths((path,))) is runs


def test_runtime_ticket_without_integration_proof_is_refused() -> None:
    result = _run(_contract(UNIT_ITEM))
    assert result.runtime_ticket
    assert not result.passed
    assert result.failure is not None
    assert "needs an integration proof" in result.failure
    assert RUNTIME[0] in result.failure


def test_runtime_ticket_with_empty_evidence_is_refused() -> None:
    assert not _run(_contract()).passed


def test_runtime_ticket_with_integration_proof_passes() -> None:
    result = _run(_contract(UNIT_ITEM, _proof_item()))
    assert result.passed
    assert result.proof_item_ids == ("dod-omn21887-integration-proof",)


@pytest.mark.parametrize(
    "command",
    [
        "uv run onex run-node node_x --input in.json",
        "uv run onex lab-work --lane l -- uv run pytest tests/test_x.py",
        "rpk topic consume t -n 1 | grep -q payload",
    ],
)
def test_live_probe_commands_are_accepted(command: str) -> None:
    assert _run(_contract(_proof_item(command=command))).passed


def test_non_runtime_ticket_owes_no_proof() -> None:
    result = _run(_contract(UNIT_ITEM), ("docs/plan.md", "tests/test_x.py"))
    assert result.passed
    assert not result.runtime_ticket


@pytest.mark.parametrize(
    "item",
    [
        _proof_item(scope="hosted_and_local"),
        _proof_item(command="uv run pytest tests/test_x.py -q"),
        _proof_item(command="echo uv run onex run-node node_x"),
        _proof_item(command="uv run onex run-node node_x || true"),
        _proof_item(check_type="file_exists", command="README.md"),
        _proof_item(item_id="dod-omn21887-unit-live"),
    ],
    ids=[
        "hosted-scope",
        "unit-test-only",
        "quoted-runner",
        "exit-laundered",
        "file-exists",
        "id-does-not-name-a-proof",
    ],
)
def test_an_item_that_does_not_prove_a_live_run_is_refused(item: str) -> None:
    result = _run(_contract(item))
    assert not result.passed


def test_a_named_but_malformed_item_is_named_in_the_refusal() -> None:
    result = _run(_contract(_proof_item(scope="hosted_and_local")))
    assert result.failure is not None
    assert "dod-omn21887-integration-proof" in result.failure


@pytest.fixture(autouse=True)
def _no_inherited_git_location(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hook exports GIT_DIR and friends; they would retarget the node's own git reads."""
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)


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
    git("checkout", "-q", "-b", "OMN-21887-work")
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / f"{TICKET}.yaml").write_text(contract)
    target = tmp_path / changed
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", f"feat({TICKET}): change")
    return git("rev-parse", "HEAD"), base


def _contract_with_ac(*items: str) -> str:
    return (
        f"ticket_id: {TICKET}\n"
        "requirements:\n"
        "  - id: req\n"
        "    statement: s\n"
        "    acceptance:\n"
        "      - id: AC1\n"
        "        statement: 'AC1: x -- falsifier: uv run pytest tests/test_x.py -q'\n"
        "dod_evidence:" + "".join(items)
    )


def test_check_refuses_a_runtime_push_without_proof_through_real_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head, base = _git_repo(tmp_path, _contract_with_ac(UNIT_ITEM), RUNTIME[0])
    monkeypatch.chdir(tmp_path)
    failures = boundary.check(head, base, f"feat({TICKET}): change")
    assert len(failures) == 1
    assert "needs an integration proof" in failures[0]


def test_check_passes_a_runtime_push_with_proof_through_real_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head, base = _git_repo(
        tmp_path, _contract_with_ac(UNIT_ITEM, _proof_item()), RUNTIME[0]
    )
    monkeypatch.chdir(tmp_path)
    assert boundary.check(head, base, f"feat({TICKET}): change") == ()


def test_check_passes_a_docs_only_push_without_proof_through_real_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head, base = _git_repo(tmp_path, _contract_with_ac(UNIT_ITEM), "docs/plan.md")
    monkeypatch.chdir(tmp_path)
    assert boundary.check(head, base, f"feat({TICKET}): change") == ()
