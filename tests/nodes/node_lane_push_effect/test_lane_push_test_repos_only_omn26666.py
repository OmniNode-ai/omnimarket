# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26666: the push node pushes to test repositories and to no other."""

from __future__ import annotations

import asyncio
import importlib
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_lane_push_effect"
)
MODELS = "omnimarket.nodes.node_lane_push_effect.models.model_lane_push"
HANDLER = "omnimarket.nodes.node_lane_push_effect.handlers.handler_lane_push"
CHECKS = "omnimarket.nodes.node_lane_push_effect.handlers.handler_lane_push_checks"
HEAD = "a" * 40
COMMIT = "c" * 40
CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
TEST_REPO = CONTRACT["lane_push"]["test_repositories"][0]


class _Calls:
    def __init__(self) -> None:
        self.calls: list[str] = []


class _Remote(_Calls):
    def read(self, repository: str, branch: str) -> str:
        self.calls.append(f"read:{repository}")
        return HEAD


class _Materializer(_Calls):
    def materialize(self, source: Any, request: Any) -> str:
        self.calls.append("materialize")
        return "/work/tree"


class _Issuer(_Calls):
    def issue(self, repository: str, correlation_id: uuid.UUID) -> object:
        self.calls.append(f"issue:{repository}")
        return object()


class _Pusher(_Calls):
    def __init__(self, models: Any) -> None:
        super().__init__()
        self._models = models

    def push(
        self, repository: str, branch: str, commit_sha: str, credential: Any
    ) -> Any:
        self.calls.append(f"push:{repository}")
        return self._models.ModelLanePushAttempt(
            exit_code=0, remote_head_after=commit_sha
        )


class _Check(_Calls):
    def __init__(self, check_id: str, models: Any) -> None:
        super().__init__()
        self.check_id = check_id
        self._models = models

    def run(self, request: Any, materialized: str) -> Any:
        self.calls.append(self.check_id)
        return self._models.ModelLanePushCheckVerdict(passed=True)


def _build(**over: Any) -> tuple[Any, Any, dict[str, Any]]:
    models = importlib.import_module(MODELS)
    handler_module = importlib.import_module(HANDLER)
    parts: dict[str, Any] = {
        "remote_head_reader": _Remote(),
        "source_materializer": _Materializer(),
        "checks": [_Check(c, models) for c in handler_module.declared_check_ids()],
        "credential_issuer": _Issuer(),
        "pusher": _Pusher(models),
    }
    parts.update(over)
    return handler_module.HandlerLanePushEffect(**parts), models, parts


def _request(models: Any, repository: str, branch: str = "omn-26666-x") -> Any:
    return models.ModelLanePushRequest(
        correlation_id=uuid.uuid4(),
        lane="pushnode2-1915",
        repository=repository,
        branch=branch,
        expected_remote_head=HEAD,
        source=models.ModelLanePushSource(
            kind="stored_ref", location="refs/lane/x/1", head_sha=COMMIT
        ),
    )


def _touched(parts: dict[str, Any]) -> list[str]:
    calls = [
        *parts["remote_head_reader"].calls,
        *parts["source_materializer"].calls,
        *parts["credential_issuer"].calls,
        *parts["pusher"].calls,
    ]
    for check in parts["checks"]:
        calls.extend(check.calls)
    return calls


@pytest.mark.parametrize(
    "repository",
    [
        "OmniNode-ai/omnimarket",
        "OmniNode-ai/omnibase_core",
        "OmniNode-ai/omnibase_infra",
        "OmniNode-ai/omniclaude",
        "OmniNode-ai/omnibase_spi",
        "SomeoneElse/onex-lane-push-test-fork",
        f"{TEST_REPO}-two",
    ],
)
def test_test_repos_only_refuses_every_other_repository_and_touches_nothing(
    repository: str,
) -> None:
    handler, models, parts = _build()
    result = asyncio.run(handler.handle(_request(models, repository)))
    assert result.outcome.value == "refused"
    assert result.refusal is not None
    assert result.refusal.value == "not_a_test_repository"
    assert _touched(parts) == []


def test_test_repos_only_control_the_declared_test_repository_is_pushed_to() -> None:
    handler, models, parts = _build()
    result = asyncio.run(handler.handle(_request(models, TEST_REPO)))
    assert result.outcome.value == "pushed"
    assert parts["pusher"].calls == [f"push:{TEST_REPO}"]


def test_test_repos_only_matches_a_github_name_in_any_case() -> None:
    handler, models, parts = _build()
    result = asyncio.run(handler.handle(_request(models, TEST_REPO.upper())))
    assert result.outcome.value == "pushed"
    assert parts["pusher"].calls == [f"push:{TEST_REPO.upper()}"]


def test_test_repos_only_contract_list_is_non_empty_and_every_name_says_test() -> None:
    names = CONTRACT["lane_push"]["test_repositories"]
    assert names
    assert [n for n in names if "test" not in n.split("/", 1)[1].casefold()] == []


@pytest.mark.parametrize(
    "names",
    [[], ["OmniNode-ai/omnimarket"], [TEST_REPO, "OmniNode-ai/omnibase_core"]],
)
def test_test_repos_only_a_handler_given_a_non_test_list_refuses_to_start(
    names: list[str],
) -> None:
    with pytest.raises(ValueError, match="test"):
        _build(test_repositories=names)


def test_test_repos_only_the_declared_list_is_what_the_handler_loads() -> None:
    handler_module = importlib.import_module(HANDLER)
    assert handler_module.declared_test_repositories() == {
        n.casefold() for n in CONTRACT["lane_push"]["test_repositories"]
    }
