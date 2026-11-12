# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dev tag lineage: a typed command through the in-memory runtime (OMN-23932)."""

from __future__ import annotations

import json
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from omnibase_core.models.validation.model_dev_tag_lineage_facts import (
    ModelDevTagLineageFacts,
)
from omnibase_core.models.validation.model_dev_tag_lineage_request import (
    ModelDevTagLineageRequest,
)
from omnibase_core.validation.checker_dev_tag_lineage import load_policy
from omnibase_core.validation.validator_dev_tag_lineage import ValidatorDevTagLineage
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(str(files("omnimarket.nodes.node_dev_tag_lineage_effect"))) / "contract.yaml"
)


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        env=scrub_git_location_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> tuple[Path, str, str, str, str]:
    repository = tmp_path / "origin"
    repository.mkdir()
    git(repository, "init", "--initial-branch=dev")
    git(repository, "config", "user.name", "Fixture")
    git(repository, "config", "user.email", "fixture@example.test")
    git(repository, "config", "uploadpack.allowFilter", "true")
    git(repository, "config", "uploadpack.allowAnySHA1InWant", "true")
    project = repository / "pyproject.toml"
    project.write_text('[project]\nname = "package"\nversion = "2.3.4"\n')
    git(repository, "add", "pyproject.toml")
    git(repository, "commit", "-m", "dev version")
    dev = git(repository, "rev-parse", "HEAD")
    tag = f"dev-20261029143607-{dev[:12]}"
    git(repository, "tag", tag)
    git(repository, "tag", "-a", f"dev-20261029143608-{dev[:12]}", "-m", "annotated")
    git(repository, "switch", "-c", "side")
    project.write_text('[project]\nname = "package"\nversion = "2.3.5"\n')
    git(repository, "add", "pyproject.toml")
    git(repository, "commit", "-m", "side version")
    side = git(repository, "rev-parse", "HEAD")
    side_tag = f"dev-20261029143609-{side[:12]}"
    git(repository, "tag", side_tag)
    git(repository, "switch", "dev")
    # The valid tag is deliberately behind the tip: ancestry needs history.
    (repository / "later.txt").write_text("later dev commit")
    git(repository, "add", "later.txt")
    git(repository, "commit", "-m", "later dev")
    return repository, dev, tag, side, side_tag


def request_for(repository: Path, tag: str) -> ModelDevTagLineageRequest:
    policy = load_policy(ValidatorDevTagLineage().contract)
    return ModelDevTagLineageRequest(
        url=repository.as_uri(), tag=tag, branch=policy.branch, policy=policy
    )


async def _run(
    tmp_path: Path, request_body: dict[str, object]
) -> tuple[RuntimeLocal, EnumWorkflowResult]:
    request = tmp_path / "request.json"
    request.write_text(json.dumps(request_body))
    runtime = RuntimeLocal(
        workflow_path=CONTRACT,
        input_path=request,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=60,
    )
    status = await runtime.run_async()
    return runtime, status


def test_golden_chain_declares_the_command_and_terminal_topics() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    assert (
        contract["runtime_dispatch"]["command_topic"]
        in contract["event_bus"]["subscribe_topics"]
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]


@pytest.mark.asyncio
async def test_golden_chain_resolves_a_real_dev_merge_tag(
    origin: tuple[Path, str, str, str, str], tmp_path: Path
) -> None:
    repository, _dev, tag, *_ = origin
    runtime, _status = await _run(
        tmp_path, request_for(repository, tag).model_dump(mode="json")
    )
    result = runtime.handler_result
    assert isinstance(result, ModelDevTagLineageFacts)
    assert result.tag_exists is True
    assert result.on_branch is True


@pytest.mark.asyncio
async def test_error_chain_keeps_partial_facts_for_an_unreachable_url(
    tmp_path: Path,
) -> None:
    request = request_for(tmp_path / "absent.git", "dev-20261029143607-000000000000")
    runtime, _status = await _run(tmp_path, request.model_dump(mode="json"))
    result = runtime.handler_result
    assert isinstance(result, ModelDevTagLineageFacts)
    assert result.tag_exists is None
    assert "ls-remote" in result.detail


@pytest.mark.asyncio
async def test_error_chain_refuses_an_invalid_request(
    origin: tuple[Path, str, str, str, str], tmp_path: Path
) -> None:
    repository, _dev, tag, *_ = origin
    body = request_for(repository, tag).model_dump(mode="json")
    runtime, status = await _run(tmp_path, {**body, "unexpected": True})
    assert status is EnumWorkflowResult.FAILED
    assert runtime.handler_result is None
