# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RED tests for OMN-26666 AC1 and AC2: a lane hands its commits to one push node.

Written before the node's models and handler exist. Each behavioural test fails with
ModuleNotFoundError for ``omnimarket.nodes.node_lane_push_effect.models`` or
``.handlers`` until the handler slice lands; that is the stated reason. The tests
marked CONTROL read only the contract and the repository's pre-commit config, and
pass today: they are the positive controls that the contract names every check and
that the tests would catch a check missing from it.

The interface the handler slice builds to, as these tests state it:

* ``ModelLanePushRequest(correlation_id, lane, repository, branch,
  expected_remote_head, source)`` with ``source`` a ``ModelLanePushSource(kind,
  location, head_sha)``, ``kind`` one of ``bundle`` or ``stored_ref``; extra fields
  forbidden, no field that holds a credential.
* ``ModelLanePushResult(outcome, correlation_id, repository, branch, ...)`` with
  ``outcome`` one of ``pushed`` / ``refused`` / ``moved``.
* ``HandlerLanePushEffect(remote_head_reader=, source_materializer=, checks=,
  credential_issuer=, pusher=)``; ``await handler.handle(request)`` returns the result.
  Every port is injected; the handler names no host, endpoint or credential.
"""

from __future__ import annotations

import importlib
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = REPO_ROOT / "src" / "omnimarket" / "nodes" / "node_lane_push_effect"
MODELS = "omnimarket.nodes.node_lane_push_effect.models.model_lane_push"
HANDLER = "omnimarket.nodes.node_lane_push_effect.handlers.handler_lane_push"

HEAD_A = "a" * 40
HEAD_B = "b" * 40
COMMIT = "c" * 40
SENTINEL_TOKEN = "sentinel-credential-value-never-in-a-result"
# The node pushes to test repositories only; the happy path runs against the contract's own.
TEST_REPO = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))[
    "lane_push"
]["test_repositories"][0]


def _contract() -> dict[str, Any]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))


def _contract_check_ids() -> list[str]:
    return [c["id"] for c in _contract()["lane_push"]["checks"]]


def _prepush_hook_ids() -> list[str]:
    config = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    return [
        hook["id"]
        for repo in config["repos"]
        for hook in repo["hooks"]
        if "pre-push" in (hook.get("stages") or [])
    ]


# --------------------------------------------------------------------------- fakes


class _Credential:
    """What the credential node's issued answer opens to; the sentinel proves no leak."""

    source = "node_github_git_credential_effect"
    token = SENTINEL_TOKEN


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []


class _RemoteHead(_Recorder):
    def __init__(self, *heads: str) -> None:
        super().__init__()
        self._heads = list(heads)

    def read(self, repository: str, branch: str) -> str:
        self.calls.append(f"read:{repository}:{branch}")
        return self._heads.pop(0) if len(self._heads) > 1 else self._heads[0]


class _Materializer(_Recorder):
    def materialize(self, source: Any, request: Any) -> str:
        self.calls.append("materialize")
        return "/work/tree"


class _Check(_Recorder):
    def __init__(self, check_id: str, *, passes: bool, models: Any) -> None:
        super().__init__()
        self.check_id = check_id
        self._passes = passes
        self._models = models

    def run(self, request: Any, materialized: str) -> Any:
        self.calls.append(self.check_id)
        return self._models.ModelLanePushCheckVerdict(
            passed=self._passes,
            finding="" if self._passes else f"{self.check_id} refused this commit",
        )


class _Issuer(_Recorder):
    def issue(self, repository: str, correlation_id: uuid.UUID) -> _Credential:
        self.calls.append(f"issue:{repository}")
        return _Credential()


class _Pusher(_Recorder):
    def __init__(self, models: Any, *, remote_after: str = COMMIT) -> None:
        super().__init__()
        self._models = models
        self._remote_after = remote_after
        self.arguments: list[tuple[str, str, str]] = []

    # No ``force`` parameter on purpose: a handler that tries to pass one fails here.
    def push(
        self, repository: str, branch: str, commit_sha: str, credential: Any
    ) -> Any:
        self.calls.append("push")
        self.arguments.append((repository, branch, commit_sha))
        return self._models.ModelLanePushAttempt(
            exit_code=0, remote_head_after=self._remote_after
        )


def _import(path: str) -> Any:
    return importlib.import_module(path)


def _request(models: Any, **over: Any) -> Any:
    values: dict[str, Any] = {
        "correlation_id": uuid.uuid4(),
        "lane": "pushnode-1850",
        "repository": TEST_REPO,
        "branch": "omn-26666-lane-push",
        "expected_remote_head": HEAD_A,
        "source": models.ModelLanePushSource(
            kind="stored_ref", location="refs/lane/pushnode-1850/1", head_sha=COMMIT
        ),
    }
    values.update(over)
    return models.ModelLanePushRequest(**values)


def _handler(
    failing: str | None = None,
    *,
    remote_heads: tuple[str, ...] = (HEAD_A,),
    remote_after: str = COMMIT,
    omit: str | None = None,
) -> tuple[Any, Any, dict[str, Any]]:
    models = _import(MODELS)
    handler_module = _import(HANDLER)
    checks = [
        _Check(check_id, passes=check_id != failing, models=models)
        for check_id in _contract_check_ids()
        if check_id != omit
    ]
    parts: dict[str, Any] = {
        "remote_head_reader": _RemoteHead(*remote_heads),
        "source_materializer": _Materializer(),
        "checks": checks,
        "credential_issuer": _Issuer(),
        "pusher": _Pusher(models, remote_after=remote_after),
    }
    return handler_module.HandlerLanePushEffect(**parts), models, parts


def _run(handler: Any, request: Any) -> Any:
    import asyncio

    return asyncio.run(handler.handle(request))


# ------------------------------------------------------------------ AC1: the hand-over


def test_ac1_request_has_no_field_that_holds_a_credential() -> None:
    models = _import(MODELS)
    fields = set(models.ModelLanePushRequest.model_fields)
    assert {
        "repository",
        "branch",
        "expected_remote_head",
        "source",
        "lane",
    } <= fields
    forbidden = ("token", "credential", "secret", "password", "key", "auth")
    assert [f for f in fields if any(word in f for word in forbidden)] == []


@pytest.mark.parametrize("extra", ["token", "credential", "password", "force"])
def test_ac1_request_refuses_a_handed_in_credential_or_force(extra: str) -> None:
    models = _import(MODELS)
    with pytest.raises(Exception, match=extra):
        _request(models, **{extra: "x"})


def test_ac1_source_is_a_bundle_or_a_stored_ref_and_nothing_else() -> None:
    models = _import(MODELS)
    for kind in ("bundle", "stored_ref"):
        models.ModelLanePushSource(kind=kind, location="x", head_sha=COMMIT)
    with pytest.raises(Exception, match="kind"):
        models.ModelLanePushSource(kind="working_tree", location="x", head_sha=COMMIT)


def test_ac1_the_answer_is_exactly_pushed_refused_or_moved() -> None:
    models = _import(MODELS)
    assert {o.value for o in models.EnumLanePushOutcome} == {
        "pushed",
        "refused",
        "moved",
    }


def test_ac1_all_checks_green_answers_pushed_and_holds_no_credential() -> None:
    handler, models, parts = _handler()
    result = _run(handler, _request(models))
    assert result.outcome.value == "pushed"
    assert result.remote_head == COMMIT
    assert parts["pusher"].arguments == [(TEST_REPO, "omn-26666-lane-push", COMMIT)]
    assert SENTINEL_TOKEN not in result.model_dump_json()
    assert SENTINEL_TOKEN not in _request(models).model_dump_json()


def test_ac1_a_moved_remote_head_answers_moved_and_asks_for_no_credential() -> None:
    handler, models, parts = _handler(remote_heads=(HEAD_B,))
    result = _run(handler, _request(models))
    assert result.outcome.value == "moved"
    assert result.remote_head == HEAD_B
    assert parts["credential_issuer"].calls == []
    assert parts["pusher"].calls == []


def test_ac1_a_head_that_moves_during_the_checks_answers_moved_at_the_push() -> None:
    handler, models, parts = _handler(remote_heads=(HEAD_A, HEAD_B))
    result = _run(handler, _request(models))
    assert result.outcome.value == "moved"
    assert parts["pusher"].calls == []


@pytest.mark.parametrize("branch", ["dev", "main"])
def test_ac1_a_protected_branch_is_refused_with_the_reason(branch: str) -> None:
    handler, models, parts = _handler()
    result = _run(handler, _request(models, branch=branch))
    assert result.outcome.value == "refused"
    assert "protected" in (result.reason or "")
    assert parts["pusher"].calls == []


def test_ac1_a_rejected_push_is_refused_never_pushed() -> None:
    handler, models, _parts = _handler(remote_after=HEAD_A)
    result = _run(handler, _request(models))
    assert result.outcome.value == "refused"
    assert "push_rejected" in (result.reason or "")


# --------------------------------------------------------------- AC2: no check skipped


def test_ac2_control_contract_names_every_prepush_hook_of_this_repository() -> None:
    declared = {c for c in _contract_check_ids() if c.startswith("pre_push:")}
    today = {f"pre_push:{h}" for h in _prepush_hook_ids()}
    assert declared == today, (
        f"hooks only in .pre-commit-config.yaml: {sorted(today - declared)}; "
        f"only in the contract: {sorted(declared - today)}"
    )


def test_ac2_control_contract_names_the_ruling_guard_and_the_release_identity() -> None:
    ids = _contract_check_ids()
    assert "ruling_guard" in ids
    assert "release_identity" in ids
    assert len(ids) == len(set(ids))


def test_ac2_control_a_hook_missing_from_the_contract_would_be_caught() -> None:
    declared = {c for c in _contract_check_ids() if c.startswith("pre_push:")}
    today = {f"pre_push:{h}" for h in _prepush_hook_ids()} | {"pre_push:a-new-hook"}
    assert declared != today


@pytest.mark.parametrize("failing", _contract_check_ids())
def test_ac2_each_check_that_refuses_stops_the_push_and_names_itself(
    failing: str,
) -> None:
    handler, models, parts = _handler(failing)
    result = _run(handler, _request(models))
    assert result.outcome.value == "refused"
    assert result.refused_check == failing
    assert failing in (result.reason or "")
    assert parts["pusher"].calls == []
    assert parts["credential_issuer"].calls == []


def test_ac2_every_declared_check_runs_on_a_green_push() -> None:
    handler, models, parts = _handler()
    _run(handler, _request(models))
    ran = [c for check in parts["checks"] for c in check.calls]
    assert sorted(ran) == sorted(_contract_check_ids())


@pytest.mark.parametrize("omitted", _contract_check_ids())
def test_ac2_a_handler_built_without_a_declared_check_refuses_to_start(
    omitted: str,
) -> None:
    _import(MODELS)
    _import(HANDLER)  # a missing module is the RED reason, not a match failure
    with pytest.raises(Exception, match=omitted.replace(":", ".")):
        _handler(omit=omitted)


def test_ac2_the_credential_is_asked_for_only_after_every_check_passed() -> None:
    handler, models, parts = _handler()
    _run(handler, _request(models))
    assert parts["credential_issuer"].calls == [f"issue:{TEST_REPO}"]
    assert parts["pusher"].calls == ["push"]
