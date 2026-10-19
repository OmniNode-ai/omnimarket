# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20910 — a container evidence check is a command on a PR writer with no docker.

``onex scm pr-create`` and the push prerequisite judge every contract command
check with the verifier's prose guard, which resolves the first token on the
writer's own PATH. The writer host has no docker, so a dev contract whose
deploy smoke starts ``docker run`` (contracts/OMN-20910.yaml,
dod-omn20910-deploy-register-key-container-smoke, and about thirty more) was
refused with ``INVALID_CHECK_VALUE_NOT_A_COMMAND: first token 'docker' is not a
resolvable executable``, so no new PR citing that ticket could be created.

The check runs on the verifier host, which declares docker, never on the
writer. The authoring prerequisite therefore accepts the verifier-host tools it
declares by name, while the verifier itself still resolves every first token on
the PATH it runs with, and prose is refused on both.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.services.contract_prerequisite import (
    VERIFIER_HOST_TOOLS,
    contract_prerequisite,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    _invalid_check_value_reason,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTRACTS = _REPO_ROOT / "contracts"
_TICKET = "OMN-20910"


@pytest.fixture
def docker_less_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PR writer host: every executable resolves except docker."""
    real_which: Callable[..., str | None] = shutil.which

    def which(cmd: str, mode: int = 1, path: str | None = None) -> str | None:
        if Path(cmd).name == "docker":
            return None
        return real_which(cmd, mode=mode, path=path)

    monkeypatch.setattr(shutil, "which", which)


def _contract(check_value: str) -> str:
    return yaml.safe_dump(
        {
            "ticket_id": _TICKET,
            "requirements": [
                {"acceptance": [{"id": "AC1", "statement": "A container smoke"}]}
            ],
            "dod_evidence": [
                {
                    "id": "dod-ac1",
                    "binds_ac": ["AC1"],
                    "checks": [{"check_type": "command", "check_value": check_value}],
                }
            ],
        }
    )


def test_docker_is_a_declared_verifier_host_tool() -> None:
    assert "docker" in VERIFIER_HOST_TOOLS


@pytest.mark.usefixtures("docker_less_host")
def test_positive_control_docker_does_not_resolve_on_the_writer() -> None:
    assert shutil.which("docker") is None
    assert shutil.which("env") is not None


@pytest.mark.usefixtures("docker_less_host")
def test_bare_docker_run_check_passes_the_prerequisite_on_a_docker_less_host() -> None:
    text = _contract('docker run --rm -v "${PWD}:/w:ro" -w /w alpine true')
    assert contract_prerequisite(_TICKET, text) is None


@pytest.mark.usefixtures("docker_less_host")
def test_prose_is_still_refused_on_a_docker_less_host() -> None:
    failure = contract_prerequisite(
        _TICKET, _contract("Verified the container manually on the lab")
    )
    assert failure is not None
    assert "INVALID_CHECK_VALUE_NOT_A_COMMAND" in failure


@pytest.mark.usefixtures("docker_less_host")
def test_verifier_still_resolves_docker_on_its_own_path() -> None:
    reason = _invalid_check_value_reason("docker run --rm alpine true")
    assert reason is not None
    assert "'docker'" in reason


@pytest.mark.usefixtures("docker_less_host")
def test_no_dev_contract_is_refused_for_docker_on_a_docker_less_host() -> None:
    refused = []
    for path in sorted(_CONTRACTS.glob("OMN-*.yaml")):
        failure = contract_prerequisite(path.stem, path.read_text())
        if failure is not None and "'docker'" in failure:
            refused.append(failure)
    assert refused == []


@pytest.mark.usefixtures("docker_less_host")
def test_omn20910_contract_passes_the_prerequisite_on_a_docker_less_host() -> None:
    text = (_CONTRACTS / f"{_TICKET}.yaml").read_text()
    assert contract_prerequisite(_TICKET, text) is None


def test_ratchet_is_wired_as_ci_step_and_pre_commit_hook() -> None:
    name = "test_contract_prerequisite_verifier_host_tools_omn20910.py"
    ci = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    hooks = (_REPO_ROOT / ".pre-commit-config.yaml").read_text()
    assert f"tests/ci/{name}" in ci
    assert f"tests/ci/{name}" in hooks
